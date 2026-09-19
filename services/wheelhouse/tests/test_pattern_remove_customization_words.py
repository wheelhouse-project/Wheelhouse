"""The window only promises restoration when this is the last customization.

wh-pattern-override-doc-id.3.6. Removing a customization deletes one saved
rule. This branch deliberately keeps every other rule that replaces the same
built-in, so the built-in comes back only when the rule being removed is the
last one replacing it. Both texts used to promise restoration in every case.

The listing supplies the missing fact as ``other_claimants``; these tests pin
the words the window shows for it. The wording is Boss e7's ruling of
2026-09-06 and is recorded in this bead's acceptance field. Two forms only,
singular and plural, and no "(s)".
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QMessageBox

# wh-pytest-flaky-segfault: constructing the dialog builds real Qt widgets;
# without a QApplication Qt aborts the whole interpreter.
pytestmark = pytest.mark.usefixtures("qapp")


LAST_COPY_TIP = (
    "Your editable copy of a built-in pattern. It replaces "
    "the built-in; Remove customization restores it."
)
LAST_COPY_CONFIRM = (
    'Remove your customized copy of "maximize"?\n\n'
    "The built-in pattern takes over again."
)
ONE_OTHER_TIP = (
    "Your editable copy of a built-in pattern. It replaces the built-in. "
    "One other customization also replaces it. Removing this copy will not "
    "bring the built-in back."
)
TWO_OTHERS_TIP = (
    "Your editable copy of a built-in pattern. It replaces the built-in. "
    "2 other customizations also replace it. Removing this copy will not "
    "bring the built-in back."
)
ONE_OTHER_CONFIRM = (
    'Remove your customized copy of "maximize"?\n\n'
    "One other customization still replaces the built-in. "
    "It will not take over again."
)
TWO_OTHERS_CONFIRM = (
    'Remove your customized copy of "maximize"?\n\n'
    "2 other customizations still replace the built-in. "
    "It will not take over again."
)


def _data(**flags):
    entry = {
        "id": "uid-maximize",
        "trigger_display": "maximize",
        "requires_hotword": False,
        "is_user_created": True,
        "overrides_builtin": True,
        "raw_pattern": "^maximize$",
        "raw_actions": [],
        "description": "Press ctrl+alt+m",
    }
    entry.update(flags)
    return {
        "hotword": "computer",
        "categories": {"User Patterns": {"patterns": [entry]}},
    }


def _selected_dialog(**flags):
    """A dialog with the one user row already selected."""
    from pattern_manager_dialog import PatternManagerDialog

    dialog = PatternManagerDialog(parent=None)
    dialog.populate(_data(**flags))
    item = dialog._tree.invisibleRootItem().child(0).child(0)
    dialog._on_selection_changed(item, None)
    return dialog


def _confirmation_text(dialog):
    """Press Remove customization and return the words the box showed."""
    with patch(
        "pattern_manager_dialog.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    ) as question:
        dialog._on_remove_customization_clicked()
    assert question.call_count == 1
    return question.call_args[0][2]


class TestTheLastCopyKeepsTodaysWords:
    """Nothing changes for the case the old wording already described."""

    def test_the_badge_tooltip_still_promises_restoration(self):
        dialog = _selected_dialog()
        assert dialog._user_badge.toolTip() == LAST_COPY_TIP

    def test_the_confirmation_still_promises_restoration(self):
        dialog = _selected_dialog()
        assert _confirmation_text(dialog) == LAST_COPY_CONFIRM


class TestOneOtherCustomizationRemains:
    """States 2, 3 and 4 of the acceptance field, which share one wording.

    Several named rules, a named rule beside a pre-doc_id one, and a
    remaining rule with an empty action list all reach the dialog as the
    same number, because the person faces the same fact in each.
    """

    def test_the_badge_tooltip_says_removing_this_copy_is_not_enough(self):
        dialog = _selected_dialog(other_claimants=1)
        assert dialog._user_badge.toolTip() == ONE_OTHER_TIP

    def test_the_confirmation_says_the_builtin_will_not_take_over(self):
        dialog = _selected_dialog(other_claimants=1)
        assert _confirmation_text(dialog) == ONE_OTHER_CONFIRM


class TestSeveralOtherCustomizationsRemain:
    def test_the_badge_tooltip_counts_them(self):
        dialog = _selected_dialog(other_claimants=2)
        assert dialog._user_badge.toolTip() == TWO_OTHERS_TIP

    def test_the_confirmation_counts_them(self):
        dialog = _selected_dialog(other_claimants=2)
        assert _confirmation_text(dialog) == TWO_OTHERS_CONFIRM

    def test_the_plural_form_carries_no_parenthesised_s(self):
        """Two forms only, as ruled. "customization(s)" is not one of them."""
        dialog = _selected_dialog(other_claimants=3)
        tip = dialog._user_badge.toolTip()
        assert "(s)" not in tip
        assert "3 other customizations" in tip


class TestWhatMustNotChange:
    def test_the_badge_text_itself_is_unchanged(self):
        """The count belongs in the hover help, not in the badge label."""
        dialog = _selected_dialog(other_claimants=2)
        assert dialog._user_badge.text() == "User (overrides built-in)"

    def test_the_button_is_still_offered_when_others_remain(self):
        """Removing this copy is still a thing the person may want to do.

        ``isVisible`` is not asserted: the dialog is never shown in these
        tests, so every widget reports False whatever the code set.
        """
        dialog = _selected_dialog(other_claimants=2)
        assert dialog._remove_custom_btn.isEnabled() is True

    def test_removing_deletes_only_this_rule(self):
        """No other saved rule is deleted to make the old sentence true."""
        dialog = _selected_dialog(other_claimants=2)
        sent = []
        dialog.pattern_action.connect(sent.append)
        with patch(
            "pattern_manager_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            dialog._on_remove_customization_clicked()
        assert sent == [
            {
                "action": "pm_delete_pattern",
                "data": {"pattern_id": "uid-maximize"},
            }
        ]

    def test_neither_text_uses_the_words_the_code_uses(self):
        """No jargon in what the person reads."""
        dialog = _selected_dialog(other_claimants=2)
        words = dialog._user_badge.toolTip() + _confirmation_text(dialog)
        lowered = words.lower()
        for jargon in ("claimant", "slot", "identity", "doc_id", "entry"):
            assert jargon not in lowered
