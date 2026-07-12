"""GUI smoke tests for the Pattern Manager dialog changes (wh-user-patterns-split.7).

Covers the split-specific additions: the system/user/override list labels, the
wake-word ("Change...") control that sends pm_set_hotword, and the delete path
sending the pattern id (not the trigger) to the Logic handler.

Also covers the detail-panel action buttons (Edit / Duplicate / Customize /
Remove customization / Explain), the try-it box under the tree
(pm_test_phrase), and the badge hover help (wh-pattern-editor-manager; spec
docs/plans/2026-07-09-pattern-manager-editor-design-v1.md section 9). The
editor dialog itself is under concurrent construction, so these tests stub
CreatePatternDialog at the manager's import site and only assert what the
manager passes in and how it wires the signals.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QShowEvent
from PySide6.QtWidgets import QMessageBox, QPushButton

# wh-pytest-flaky-segfault: constructing the dialog builds real Qt widgets;
# without a QApplication Qt aborts the whole interpreter. The session-scoped
# qapp fixture guarantees one exists even when this file runs alone.
pytestmark = pytest.mark.usefixtures("qapp")


def _sample_data():
    return {
        "hotword": "computer",
        "categories": {
            "Commands - Window Management": {
                "patterns": [
                    {
                        "id": "sysid",
                        "trigger_display": "save",
                        "requires_hotword": False,
                        "is_user_created": False,
                        "overrides_builtin": False,
                        "raw_pattern": "^save$",
                        "raw_actions": [],
                        "description": "Press Ctrl+S",
                    },
                ]
            },
            "User Patterns": {
                "patterns": [
                    {
                        "id": "uid-deploy",
                        "trigger_display": "deploy",
                        "requires_hotword": False,
                        "is_user_created": True,
                        "overrides_builtin": False,
                        "raw_pattern": "^deploy$",
                        "raw_actions": [],
                        "description": "Press Ctrl+D",
                    },
                    {
                        "id": "uid-save",
                        "trigger_display": "save",
                        "requires_hotword": False,
                        "is_user_created": True,
                        "overrides_builtin": True,
                        "raw_pattern": "^save$",
                        "raw_actions": [],
                        "description": "Press F5",
                    },
                ]
            },
        },
    }


def _make_dialog():
    from pattern_manager_dialog import PatternManagerDialog
    return PatternManagerDialog(parent=None)


def _all_child_labels(dialog):
    labels = []
    root = dialog._tree.invisibleRootItem()
    for i in range(root.childCount()):
        cat = root.child(i)
        for j in range(cat.childCount()):
            labels.append(cat.child(j).text(0))
    return labels


def test_populate_updates_wake_word_label():
    dialog = _make_dialog()
    dialog.populate(_sample_data())
    assert dialog._hotword_value.text() == "computer"


def test_list_labels_mark_user_and_override():
    dialog = _make_dialog()
    dialog.populate(_sample_data())
    labels = _all_child_labels(dialog)
    # Built-in save: no user/override tag.
    assert any(lbl == "save" for lbl in labels)
    # Plain user pattern: [user].
    assert any("deploy" in lbl and "[user]" in lbl for lbl in labels)
    # User override of a built-in: [overrides built-in].
    assert any("save" in lbl and "[overrides built-in]" in lbl for lbl in labels)


def test_change_hotword_emits_set_hotword():
    dialog = _make_dialog()
    emitted = []
    dialog.pattern_action.connect(emitted.append)
    with patch(
        "PySide6.QtWidgets.QInputDialog.getText", return_value=("jarvis", True)
    ):
        dialog._on_change_hotword_clicked()
    assert emitted == [
        {"action": "pm_set_hotword", "data": {"hotword": "jarvis"}}
    ]


def test_change_hotword_rejects_empty_without_emitting():
    # Field-level error under the wake-word row, not a modal box
    # (wh-pattern-editor-ux, spec section 13).
    dialog = _make_dialog()
    emitted = []
    dialog.pattern_action.connect(emitted.append)
    with patch(
        "PySide6.QtWidgets.QInputDialog.getText", return_value=("   ", True)
    ), patch("pattern_manager_dialog.QMessageBox.warning") as warn:
        dialog._on_change_hotword_clicked()
    warn.assert_not_called()
    assert not dialog._hotword_error_label.isHidden()
    assert "empty" in dialog._hotword_error_label.text().lower()
    assert emitted == []


def test_change_hotword_cancel_does_not_emit():
    dialog = _make_dialog()
    emitted = []
    dialog.pattern_action.connect(emitted.append)
    with patch(
        "PySide6.QtWidgets.QInputDialog.getText", return_value=("jarvis", False)
    ):
        dialog._on_change_hotword_clicked()
    assert emitted == []


def test_delete_emits_pattern_id():
    dialog = _make_dialog()
    dialog._selected_pattern = {
        "id": "uid-deploy",
        "trigger_display": "deploy",
        "is_user_created": True,
    }
    emitted = []
    dialog.pattern_action.connect(emitted.append)
    with patch(
        "pattern_manager_dialog.QMessageBox.question",
        return_value=QMessageBox.StandardButton.Yes,
    ):
        dialog._on_delete_clicked()
    assert emitted == [
        {"action": "pm_delete_pattern", "data": {"pattern_id": "uid-deploy"}}
    ]


# ---------------------------------------------------------------------------
# wh-pattern-editor-manager helpers
# ---------------------------------------------------------------------------


def _select_pattern(dialog, pattern_id):
    """Select the tree item carrying ``pattern_id``; return its entry dict."""
    root = dialog._tree.invisibleRootItem()
    for i in range(root.childCount()):
        cat = root.child(i)
        for j in range(cat.childCount()):
            child = cat.child(j)
            pat = child.data(0, Qt.ItemDataRole.UserRole)
            if pat and pat.get("id") == pattern_id:
                dialog._tree.setCurrentItem(child)
                return pat
    raise AssertionError(f"pattern {pattern_id} not found in tree")


def _fake_editor_class(manager, record):
    """A CreatePatternDialog stand-in that records its constructor args and
    the manager's ``_editor_dialog`` value while exec() runs."""

    class FakeEditor:
        def __init__(self, hotword, parent=None, entry=None, pattern_id=None):
            record.update(
                hotword=hotword,
                parent=parent,
                entry=entry,
                pattern_id=pattern_id,
                editor=self,
            )
            self.pattern_action = MagicMock()
            # _open_editor's cleanup (wh-pattern-editor-r0.6) stops the
            # editor's timers and schedules deletion after exec().
            self._try_timer = MagicMock()
            self._save_timeout_timer = MagicMock()
            self.deleteLater = MagicMock()

        def exec(self):
            record["editor_during_exec"] = manager._editor_dialog
            return 0

    return FakeEditor


# ---------------------------------------------------------------------------
# Detail-panel action buttons: enable/visibility follow the selection
# ---------------------------------------------------------------------------


class TestDetailButtons:
    def test_no_selection_disables_action_buttons(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        for btn in (
            dialog._edit_btn,
            dialog._duplicate_btn,
            dialog._customize_btn,
            dialog._remove_custom_btn,
            dialog._explain_btn,
        ):
            assert not btn.isEnabled()

    def test_builtin_selection_button_states(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        _select_pattern(dialog, "sysid")
        assert not dialog._edit_btn.isEnabled()
        assert dialog._duplicate_btn.isEnabled()
        assert dialog._explain_btn.isEnabled()
        assert not dialog._customize_btn.isHidden()
        assert dialog._customize_btn.isEnabled()
        assert dialog._remove_custom_btn.isHidden()
        assert not dialog._delete_btn.isEnabled()

    def test_user_selection_button_states(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        _select_pattern(dialog, "uid-deploy")
        assert dialog._edit_btn.isEnabled()
        assert dialog._duplicate_btn.isEnabled()
        assert dialog._explain_btn.isEnabled()
        assert dialog._customize_btn.isHidden()
        assert dialog._remove_custom_btn.isHidden()
        assert dialog._delete_btn.isEnabled()

    def test_override_selection_button_states(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        _select_pattern(dialog, "uid-save")
        assert dialog._edit_btn.isEnabled()
        assert dialog._customize_btn.isHidden()
        assert not dialog._remove_custom_btn.isHidden()
        assert dialog._remove_custom_btn.isEnabled()

    def test_new_controls_have_accessible_names(self):
        dialog = _make_dialog()
        for widget in (
            dialog._edit_btn,
            dialog._duplicate_btn,
            dialog._customize_btn,
            dialog._remove_custom_btn,
            dialog._explain_btn,
            dialog._try_input,
        ):
            assert widget.accessibleName() != ""


# ---------------------------------------------------------------------------
# Edit / Duplicate / Customize open the editor with the right arguments
# ---------------------------------------------------------------------------


class TestOpenEditorWiring:
    def _dialog_with_selection(self, pattern_id):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        pat = _select_pattern(dialog, pattern_id)
        return dialog, pat

    def test_edit_opens_editor_with_entry_and_pattern_id(self):
        dialog, pat = self._dialog_with_selection("uid-deploy")
        record = {}
        with patch(
            "create_pattern_dialog.CreatePatternDialog",
            _fake_editor_class(dialog, record),
        ):
            dialog._edit_btn.click()
        assert record["hotword"] == "computer"
        assert record["parent"] is dialog
        assert record["entry"] == pat
        assert record["pattern_id"] == "uid-deploy"
        # Same lifecycle as Add: _editor_dialog set around exec, then cleared.
        assert record["editor_during_exec"] is record["editor"]
        assert dialog._editor_dialog is None
        assert record["editor"].pattern_action.connect.call_count == 1

    def test_duplicate_opens_editor_without_pattern_id(self):
        dialog, pat = self._dialog_with_selection("uid-save")
        record = {}
        with patch(
            "create_pattern_dialog.CreatePatternDialog",
            _fake_editor_class(dialog, record),
        ):
            dialog._duplicate_btn.click()
        assert record["entry"] == pat
        assert record["pattern_id"] is None
        assert dialog._editor_dialog is None

    def test_customize_opens_editor_without_pattern_id(self):
        dialog, pat = self._dialog_with_selection("sysid")
        record = {}
        with patch(
            "create_pattern_dialog.CreatePatternDialog",
            _fake_editor_class(dialog, record),
        ):
            dialog._customize_btn.click()
        assert record["entry"] == pat
        assert record["pattern_id"] is None
        assert dialog._editor_dialog is None


# ---------------------------------------------------------------------------
# Remove customization: confirm, then pm_delete_pattern for the user copy
# ---------------------------------------------------------------------------


class TestRemoveCustomization:
    def test_confirm_yes_emits_delete_for_override(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        _select_pattern(dialog, "uid-save")
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        with patch(
            "pattern_manager_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            dialog._remove_custom_btn.click()
        assert emitted == [
            {"action": "pm_delete_pattern", "data": {"pattern_id": "uid-save"}}
        ]

    def test_confirm_no_emits_nothing(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        _select_pattern(dialog, "uid-save")
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        with patch(
            "pattern_manager_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ):
            dialog._remove_custom_btn.click()
        assert emitted == []


# ---------------------------------------------------------------------------
# Explain panel and the explainer as the detail wording source
# ---------------------------------------------------------------------------


class TestExplainPanel:
    def test_explain_button_shows_explainer_text(self):
        from speech.pattern_explainer import explain_pattern

        dialog = _make_dialog()
        dialog.populate(_sample_data())
        pat = _select_pattern(dialog, "sysid")
        dialog._explain_btn.click()
        assert not dialog._explain_group.isHidden()
        assert dialog._explain_text.toPlainText() == explain_pattern(
            pat, "computer"
        )

    def test_explain_toggle_hides_panel(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        _select_pattern(dialog, "sysid")
        dialog._explain_btn.click()
        dialog._explain_btn.click()
        assert dialog._explain_group.isHidden()

    def test_explain_panel_follows_selection(self):
        from speech.pattern_explainer import explain_pattern

        dialog = _make_dialog()
        dialog.populate(_sample_data())
        _select_pattern(dialog, "sysid")
        dialog._explain_btn.click()
        pat = _select_pattern(dialog, "uid-deploy")
        assert dialog._explain_text.toPlainText() == explain_pattern(
            pat, "computer"
        )

    def test_repopulate_hides_explain_panel(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        _select_pattern(dialog, "sysid")
        dialog._explain_btn.click()
        dialog.populate(_sample_data())
        assert dialog._explain_group.isHidden()
        assert not dialog._explain_btn.isChecked()

    def test_type_badge_classifies_replacement_by_anchor(self):
        # Shipped replacements (\bperiod\b etc.) and simple-mode text
        # patterns carry no explicit type key; the badge must classify by
        # the same precedence the explainer uses instead of defaulting to
        # "Command" (wh-pattern-editor-r4.2).
        dialog = _make_dialog()
        dialog._show_detail(
            {
                "id": "rid",
                "trigger_display": "period",
                "raw_pattern": r"\bperiod\b",
                "raw_actions": [{"function": "text", "params": ["."]}],
            }
        )
        assert dialog._type_badge.text() == "Replacement"
        assert dialog._detail_type.text() == "Replacement"

    def test_type_badge_command_by_anchor_and_stored_type_wins(self):
        dialog = _make_dialog()
        dialog._show_detail(
            {
                "id": "cid",
                "trigger_display": "save",
                "raw_pattern": "^save$",
                "raw_actions": [{"function": "hk", "params": ["ctrl", "s"]}],
            }
        )
        assert dialog._type_badge.text() == "Command"
        dialog._show_detail(
            {
                "id": "tid",
                "trigger_display": "deploy",
                "raw_pattern": "^deploy$",
                "type": "command",
                "raw_actions": [{"function": "hk", "params": ["ctrl", "d"]}],
            }
        )
        assert dialog._type_badge.text() == "Command"

    def test_type_badge_trailing_position(self):
        dialog = _make_dialog()
        dialog._show_detail(
            {
                "id": "trid",
                "trigger_display": "submit",
                "raw_pattern": "submit",
                "position": "trailing",
                "raw_actions": [{"function": "press", "params": ["enter"]}],
            }
        )
        assert dialog._type_badge.text() == "Trailing command"
        assert dialog._detail_type.text() == "Trailing command"

    def test_detail_action_prefers_logic_description(self):
        dialog = _make_dialog()
        dialog._show_detail(
            {
                "id": "sysid",
                "trigger_display": "save",
                "raw_pattern": "^save$",
                "raw_actions": [{"function": "hk", "params": ["ctrl", "s"]}],
                "description": "Press Ctrl+S",
            }
        )
        assert dialog._detail_action.text() == "Press Ctrl+S"

    def test_detail_action_falls_back_to_explainer(self):
        from speech.pattern_explainer import explain_pattern

        entry = {
            "id": "b" * 64,
            "trigger_display": "deploy",
            "requires_hotword": False,
            "is_user_created": True,
            "overrides_builtin": False,
            "raw_pattern": "^deploy$",
            "raw_actions": [{"function": "hk", "params": ["ctrl", "d"]}],
        }
        dialog = _make_dialog()
        dialog._show_detail(entry)
        assert dialog._detail_action.text() == explain_pattern(entry, "x-ray")


# ---------------------------------------------------------------------------
# Try-it box under the tree (pm_test_phrase)
# ---------------------------------------------------------------------------


class TestTryItBox:
    def test_typing_starts_debounce_and_clearing_stops(self):
        dialog = _make_dialog()
        dialog._try_input.setText("save")
        assert dialog._try_timer.isActive()
        dialog._try_input.setText("")
        assert not dialog._try_timer.isActive()
        assert dialog._try_result_label.text() == ""

    def test_enter_sends_pm_test_phrase_once(self):
        dialog = _make_dialog()
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._try_input.setText("open browser")
        dialog._try_input.returnPressed.emit()
        assert emitted == [
            {"action": "pm_test_phrase",
             "data": {"text": "open browser", "request_id": 1}}
        ]
        # Enter cancels the pending debounce so nothing sends twice.
        assert not dialog._try_timer.isActive()

    def test_debounce_send_uses_stripped_text(self):
        dialog = _make_dialog()
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._try_input.setText("  save  ")
        dialog._send_test_phrase()
        assert emitted == [
            {"action": "pm_test_phrase",
             "data": {"text": "save", "request_id": 1}}
        ]

    def test_empty_text_sends_nothing(self):
        dialog = _make_dialog()
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._send_test_phrase()
        assert emitted == []

    def test_match_result_selects_pattern_and_names_wake_word(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        dialog._try_input.setText("save")
        dialog.handle_response(
            {
                "action": "pm_test_phrase_result",
                "data": {
                    "success": True,
                    "match": {
                        "pattern_id": "uid-save",
                        "trigger_display": "save",
                        "requires_hotword": True,
                        "groups": [],
                        "resolved_steps": [],
                        "is_user_created": True,
                    },
                },
            }
        )
        current = dialog._tree.currentItem()
        assert current is not None
        pat = current.data(0, Qt.ItemDataRole.UserRole)
        assert pat["id"] == "uid-save"
        label = dialog._try_result_label.text()
        assert "save" in label
        assert "computer" in label  # the wake word is named

    def test_match_result_without_wake_word(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        dialog._try_input.setText("deploy")
        dialog.handle_response(
            {
                "action": "pm_test_phrase_result",
                "data": {
                    "success": True,
                    "match": {
                        "pattern_id": "uid-deploy",
                        "trigger_display": "deploy",
                        "requires_hotword": False,
                        "groups": [],
                        "resolved_steps": [],
                        "is_user_created": True,
                    },
                },
            }
        )
        assert "no wake word" in dialog._try_result_label.text().lower()

    def test_match_with_unknown_id_still_reports(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        dialog._try_input.setText("save")
        dialog.handle_response(
            {
                "action": "pm_test_phrase_result",
                "data": {
                    "success": True,
                    "match": {
                        "pattern_id": "gone",
                        "trigger_display": "save",
                        "requires_hotword": False,
                    },
                },
            }
        )
        assert "save" in dialog._try_result_label.text()

    def test_no_match_result_plain_message(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        dialog._try_input.setText("zzz unknown")
        dialog.handle_response(
            {
                "action": "pm_test_phrase_result",
                "data": {"success": True, "match": None},
            }
        )
        label = dialog._try_result_label.text()
        assert "no pattern" in label.lower()
        assert "zzz unknown" in label


class TestTryPhraseCorrelation:
    """A try-it answer renders only against the request that produced it
    (wh-pattern-editor-r6.1): every pm_test_phrase carries an increasing
    request_id, a result carrying an out-of-date id is dropped, and a
    current result names the text that was actually tested."""

    def _phrase_result(self, request_id=None, match=None):
        data = {"success": True, "match": match}
        if request_id is not None:
            data["request_id"] = request_id
        return {"action": "pm_test_phrase_result", "data": data}

    def test_requests_carry_increasing_request_ids(self):
        dialog = _make_dialog()
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._try_input.setText("save")
        dialog._send_test_phrase()
        dialog._try_input.setText("deploy")
        dialog._send_test_phrase()
        ids = [m["data"]["request_id"] for m in emitted]
        assert ids == [1, 2]

    def test_out_of_date_result_is_dropped(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        dialog._try_input.setText("save")
        dialog._send_test_phrase()
        dialog._try_input.setText("deploy")
        dialog._send_test_phrase()
        selected_before = dialog._tree.currentItem()
        match = {"pattern_id": "uid-save", "trigger_display": "save",
                 "requires_hotword": False}
        dialog.handle_response(self._phrase_result(request_id=1, match=match))
        assert dialog._try_result_label.text() == ""
        assert dialog._tree.currentItem() is selected_before

    def test_current_result_renders_sent_text_not_box_text(self):
        # The box was edited after the send; the answer is about the SENT
        # text, and the pending debounce refreshes for the new text.
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        dialog._try_input.setText("save")
        dialog._send_test_phrase()
        dialog._try_input.setText("deploy")
        dialog.handle_response(self._phrase_result(request_id=1, match=None))
        label = dialog._try_result_label.text()
        assert "'save'" in label
        assert "deploy" not in label

    def test_result_without_request_id_still_renders(self):
        # Compatibility: an id-less result keeps the old tolerant path
        # (handler failure envelopes and older Logic processes carry none).
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        dialog._try_input.setText("zzz")
        dialog.handle_response(self._phrase_result(match=None))
        assert "no pattern" in dialog._try_result_label.text().lower()

    def test_clearing_input_invalidates_in_flight_result(self):
        # Clearing the box is a deliberate reset; an answer still in
        # flight must not reappear in the cleared box or move the tree
        # selection (wh-pattern-editor-r7.1).
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        dialog._try_input.setText("save")
        dialog._send_test_phrase()
        selected_before = dialog._tree.currentItem()
        dialog._try_input.setText("")
        match = {"pattern_id": "uid-save", "trigger_display": "save",
                 "requires_hotword": False}
        dialog.handle_response(self._phrase_result(request_id=1, match=match))
        assert dialog._try_result_label.text() == ""
        assert dialog._tree.currentItem() is selected_before


# ---------------------------------------------------------------------------
# Badge hover help
# ---------------------------------------------------------------------------


class TestBadgeTooltips:
    def test_tree_marker_tooltips(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        tips = {}
        root = dialog._tree.invisibleRootItem()
        for i in range(root.childCount()):
            cat = root.child(i)
            for j in range(cat.childCount()):
                child = cat.child(j)
                pat = child.data(0, Qt.ItemDataRole.UserRole)
                tips[pat["id"]] = child.toolTip(0)
        assert "built-in" in tips["uid-save"]
        assert tips["uid-deploy"] != ""

    def test_detail_badge_tooltips(self):
        dialog = _make_dialog()
        assert dialog._type_badge.toolTip() != ""
        assert dialog._hotword_badge.toolTip() != ""
        dialog.populate(_sample_data())
        _select_pattern(dialog, "uid-save")
        assert "built-in" in dialog._user_badge.toolTip()
        _select_pattern(dialog, "uid-deploy")
        assert dialog._user_badge.toolTip() != ""
        assert "built-in" not in dialog._user_badge.toolTip()


# ---------------------------------------------------------------------------
# UX quality pass (wh-pattern-editor-ux, spec section 13)
# ---------------------------------------------------------------------------


def _focus_chain(start, limit=800):
    """Every widget in start's window focus chain, starting at start."""
    chain = [start]
    widget = start.nextInFocusChain()
    while widget is not start and len(chain) < limit:
        chain.append(widget)
        widget = widget.nextInFocusChain()
    return chain


def _assert_in_order(chain, widgets):
    positions = []
    for widget in widgets:
        assert widget in chain, f"{widget!r} missing from focus chain"
        positions.append(chain.index(widget))
    assert positions == sorted(positions), (
        f"focus order wrong: {positions} for {widgets}"
    )


class TestManagerKeyboard:
    def test_no_button_is_autodefault(self):
        # QPushButton in a QDialog defaults autoDefault True: Enter in the
        # filter box would click an arbitrary button. The manager has no
        # unambiguous default action, so no button may be autoDefault.
        dialog = _make_dialog()
        buttons = dialog.findChildren(QPushButton)
        assert buttons
        offenders = [b.text() for b in buttons if b.autoDefault()]
        assert not offenders

    def test_enter_in_filter_moves_focus_to_tree(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        dialog._filter_input.setFocus()
        dialog._filter_input.returnPressed.emit()
        assert dialog.focusWidget() is dialog._tree

    def test_tab_order_walks_major_controls_in_order(self):
        dialog = _make_dialog()
        chain = _focus_chain(dialog._change_hw_btn)
        _assert_in_order(chain, [
            dialog._change_hw_btn,
            dialog._filter_input,
            dialog._tree,
            dialog._try_input,
            dialog._add_btn,
            dialog._help_btn,
            dialog._edit_btn,
            dialog._duplicate_btn,
            dialog._customize_btn,
            dialog._remove_custom_btn,
            dialog._explain_btn,
            dialog._advanced_toggle,
            dialog._delete_btn,
        ])

    def test_readonly_text_areas_release_tab(self):
        # Tab must move focus out of the read-only text panes, never get
        # swallowed by them (keyboard-first, spec section 13).
        dialog = _make_dialog()
        for widget in (
            dialog._explain_text, dialog._raw_regex, dialog._raw_actions,
        ):
            assert widget.tabChangesFocus()


class TestManagerEmptyStates:
    def test_filter_with_no_match_shows_empty_state(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        label = dialog._tree_empty_label
        assert label.isHidden()
        dialog._filter_input.setText("zzz-nothing")
        assert not label.isHidden()
        assert "No patterns match 'zzz-nothing'" in label.text()
        dialog._filter_input.setText("save")
        assert label.isHidden()
        dialog._filter_input.setText("zzz-nothing")
        dialog._filter_input.setText("")
        assert label.isHidden()

    def test_placeholder_prompts_selection(self):
        dialog = _make_dialog()
        assert not dialog._placeholder_label.isHidden()
        assert "Select a pattern" in dialog._placeholder_label.text()

    def test_close_event_hides_and_preserves_splitter(self):
        # The manager hides instead of closing (closeEvent ignores), so
        # the splitter widget -- and its drag state -- survives reopen.
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        dialog.resize(900, 560)
        dialog._splitter.setSizes([300, 520])
        before = dialog._splitter.sizes()
        event = QCloseEvent()
        dialog.closeEvent(event)
        assert not event.isAccepted()
        assert dialog.isHidden()
        assert dialog._splitter.sizes() == before


class TestHotwordFieldError:
    def test_set_hotword_failure_shows_field_error_not_modal(self):
        dialog = _make_dialog()
        with patch("pattern_manager_dialog.QMessageBox.warning") as warn:
            dialog.handle_response({
                "action": "pm_set_hotword_result",
                "data": {
                    "success": False,
                    "error": "The wake word must be a single word",
                },
            })
        warn.assert_not_called()
        assert not dialog._hotword_error_label.isHidden()
        assert (
            dialog._hotword_error_label.text()
            == "The wake word must be a single word"
        )

    def test_hotword_error_cleared_on_success(self):
        # QMessageBox.warning stays patched so a regression to the modal
        # cannot block the offscreen run.
        dialog = _make_dialog()
        with patch("pattern_manager_dialog.QMessageBox.warning"):
            dialog.handle_response({
                "action": "pm_set_hotword_result",
                "data": {"success": False, "error": "nope"},
            })
            assert not dialog._hotword_error_label.isHidden()
            dialog.handle_response({
                "action": "pm_set_hotword_result",
                "data": {"success": True},
            })
        assert dialog._hotword_error_label.isHidden()

    def test_hotword_error_cleared_on_new_attempt(self):
        dialog = _make_dialog()
        with patch("pattern_manager_dialog.QMessageBox.warning"):
            dialog.handle_response({
                "action": "pm_set_hotword_result",
                "data": {"success": False, "error": "nope"},
            })
        with patch(
            "PySide6.QtWidgets.QInputDialog.getText",
            return_value=("jarvis", True),
        ):
            dialog._on_change_hotword_clicked()
        assert dialog._hotword_error_label.isHidden()


class TestManagerAccessibilityAndTooltips:
    def test_every_interactive_control_named_and_described(self):
        dialog = _make_dialog()
        for widget in (
            dialog._change_hw_btn,
            dialog._filter_input,
            dialog._tree,
            dialog._try_input,
            dialog._add_btn,
            dialog._help_btn,
            dialog._edit_btn,
            dialog._duplicate_btn,
            dialog._customize_btn,
            dialog._remove_custom_btn,
            dialog._explain_btn,
            dialog._explain_text,
            dialog._advanced_toggle,
            dialog._raw_regex,
            dialog._raw_actions,
            dialog._delete_btn,
        ):
            assert widget.accessibleName() != "", widget
            assert widget.accessibleDescription() != "", widget

    def test_tooltips_on_flagged_controls(self):
        # The hover-help sweep gaps called out by the spec (section 13):
        # filter box, wake-word row, plus the window-level buttons.
        dialog = _make_dialog()
        for widget in (
            dialog._filter_input,
            dialog._change_hw_btn,
            dialog._hotword_value,
            dialog._add_btn,
            dialog._help_btn,
            dialog._delete_btn,
            dialog._advanced_toggle,
        ):
            assert widget.toolTip() != "", widget


class TestManagerLoadTimeout:
    """A lost pm_patterns_data must not leave a permanently empty window
    with no explanation (wh-pattern-editor-r0.2, manager side)."""

    def test_show_event_with_empty_tree_starts_timer(self):
        dialog = _make_dialog()
        dialog.showEvent(QShowEvent())
        assert dialog._load_timer.isActive()

    def test_show_event_with_populated_tree_does_not_start_timer(self):
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        dialog.showEvent(QShowEvent())
        assert not dialog._load_timer.isActive()

    def test_populate_cancels_pending_timer(self):
        dialog = _make_dialog()
        dialog.showEvent(QShowEvent())
        assert dialog._load_timer.isActive()
        dialog.populate(_sample_data())
        assert not dialog._load_timer.isActive()

    def test_timeout_shows_load_error_state(self):
        dialog = _make_dialog()
        dialog.showEvent(QShowEvent())
        dialog._on_load_timeout()
        label = dialog._tree_empty_label
        assert not label.isHidden()
        assert "Could not load patterns" in label.text()
        assert "reopen" in label.text()

    def test_late_populate_clears_load_error_state(self):
        dialog = _make_dialog()
        dialog.showEvent(QShowEvent())
        dialog._on_load_timeout()
        dialog.populate(_sample_data())
        assert dialog._tree_empty_label.isHidden()
        assert dialog._tree_empty_label.text() == ""


class TestGetPatternsFailure:
    """pm_get_patterns failure envelopes must not leave the manager
    silently empty forever (wh-pattern-editor-r0.3)."""

    def test_failure_envelope_shows_load_error_with_text(self):
        dialog = _make_dialog()
        dialog.showEvent(QShowEvent())
        dialog.handle_response({
            "action": "pm_get_patterns_result",
            "data": {"success": False, "error": "patterns file unreadable"},
        })
        label = dialog._tree_empty_label
        assert not label.isHidden()
        assert "patterns file unreadable" in label.text()
        # The response arrived (as an error); the watchdog must not later
        # overwrite the specific message with the generic timeout one.
        assert not dialog._load_timer.isActive()

    def test_success_envelope_shows_no_error(self):
        dialog = _make_dialog()
        dialog.handle_response({
            "action": "pm_get_patterns_result",
            "data": {"success": True},
        })
        assert dialog._tree_empty_label.isHidden()


class TestUpdateFailureModalFallback:
    """pm_update_result failure with the editor already closed must not be
    silent; mirror the create path's modal fallback
    (wh-pattern-editor-r0.3)."""

    def test_update_failure_without_editor_shows_modal(self):
        dialog = _make_dialog()
        with patch("pattern_manager_dialog.QMessageBox.warning") as warn:
            dialog.handle_response({
                "action": "pm_update_result",
                "data": {"success": False, "error": "nope"},
            })
        warn.assert_called_once()

    def test_update_failure_with_editor_open_shows_no_modal(self):
        dialog = _make_dialog()
        editor = MagicMock()
        dialog._editor_dialog = editor
        message = {
            "action": "pm_update_result",
            "data": {"success": False, "error": "nope"},
        }
        with patch("pattern_manager_dialog.QMessageBox.warning") as warn:
            dialog.handle_response(message)
        warn.assert_not_called()
        editor.handle_response.assert_called_once_with(message)


class TestManagerBanner:
    """One reusable top banner (wh-pattern-editor-r0.7/r0.8 GUI side):
    error style for a corrupt user patterns file reported on
    pm_patterns_data, warning style for a "warning" field riding a
    successful pm_*_result envelope (file write landed, live reload
    failed)."""

    def _file_error_data(self, backup_path):
        data = _sample_data()
        data["user_file_error"] = {
            "path": "C:\\data\\user_patterns.toml",
            "error": "Invalid TOML: expected ']' at line 7",
            "backup_path": backup_path,
        }
        return data

    def test_banner_hidden_by_default(self):
        dialog = _make_dialog()
        assert dialog._banner_label.isHidden()

    def test_user_file_error_shows_error_banner_with_backup_wording(self):
        dialog = _make_dialog()
        dialog.populate(
            self._file_error_data("C:\\data\\user_patterns.toml.bak")
        )
        banner = dialog._banner_label
        assert not banner.isHidden()
        assert "user_patterns.toml" in banner.text()
        assert "expected ']' at line 7" in banner.text()
        assert "user_patterns.toml.bak" in banner.text()
        assert "previous version" in banner.text().lower()

    def test_user_file_error_without_backup_omits_backup_wording(self):
        dialog = _make_dialog()
        dialog.populate(self._file_error_data(None))
        banner = dialog._banner_label
        assert not banner.isHidden()
        assert ".bak" not in banner.text()
        assert "previous version" not in banner.text().lower()

    def test_warning_on_success_envelope_shows_warning_banner(self):
        dialog = _make_dialog()
        dialog.handle_response({
            "action": "pm_update_result",
            "data": {
                "success": True,
                "warning": "Saved, but the live pattern reload failed",
            },
        })
        assert not dialog._banner_label.isHidden()
        assert "live pattern reload failed" in dialog._banner_label.text()

    def test_warning_absent_on_failure_envelope(self):
        # "warning" is defined only on success envelopes (contract 2);
        # a failure's error text goes through the normal error paths.
        dialog = _make_dialog()
        editor = MagicMock()
        dialog._editor_dialog = editor
        dialog.handle_response({
            "action": "pm_update_result",
            "data": {"success": False, "error": "nope"},
        })
        assert dialog._banner_label.isHidden()

    def test_error_and_warning_styles_differ(self):
        dialog = _make_dialog()
        dialog.populate(self._file_error_data(None))
        error_style = dialog._banner_label.styleSheet()
        dialog.handle_response({
            "action": "pm_create_result",
            "data": {"success": True, "warning": "reload failed"},
        })
        assert dialog._banner_label.styleSheet() != error_style

    def test_clean_populate_clears_banner(self):
        dialog = _make_dialog()
        dialog.populate(self._file_error_data(None))
        assert not dialog._banner_label.isHidden()
        dialog.populate(_sample_data())
        assert dialog._banner_label.isHidden()
        assert dialog._banner_label.text() == ""

    def test_warning_banner_survives_the_post_save_refresh(self):
        # A successful save immediately re-requests patterns, and the
        # clean populate that answers it must NOT wipe the reload
        # warning before the user can read it; only the error banner
        # (corrupt user file) is owned by populate data.
        dialog = _make_dialog()
        dialog.handle_response({
            "action": "pm_create_result",
            "data": {"success": True, "warning": "Saved, but stale"},
        })
        dialog.populate(_sample_data())
        assert not dialog._banner_label.isHidden()
        assert "Saved, but stale" in dialog._banner_label.text()

    def test_warning_banner_cleared_by_warning_free_save(self):
        # A later save whose live reload worked proves the running
        # patterns are fresh again, so the stale-reload warning goes.
        dialog = _make_dialog()
        dialog.handle_response({
            "action": "pm_create_result",
            "data": {"success": True, "warning": "Saved, but stale"},
        })
        dialog.handle_response({
            "action": "pm_update_result",
            "data": {"success": True},
        })
        assert dialog._banner_label.isHidden()

    def test_warning_banner_not_cleared_by_test_phrase_result(self):
        # Try-it results say nothing about the live reload state.
        dialog = _make_dialog()
        dialog.handle_response({
            "action": "pm_create_result",
            "data": {"success": True, "warning": "Saved, but stale"},
        })
        dialog.handle_response({
            "action": "pm_test_phrase_result",
            "data": {"success": True, "match": None},
        })
        assert not dialog._banner_label.isHidden()


class TestGenericFailureEnvelopeNames:
    """The generic Logic failure handler names its envelope
    f"{action}_result" ("pm_create_pattern_result"), while the branch
    handlers answer with the short names ("pm_create_result"). A
    pre-handler failure (e.g. the speech handler not ready yet) must
    reach the same error display, not vanish unhandled."""

    @pytest.mark.parametrize("long_name,title_word", [
        ("pm_create_pattern_result", "Creating"),
        ("pm_update_pattern_result", "Updating"),
        ("pm_delete_pattern_result", "Deleting"),
    ])
    def test_long_form_failure_reaches_the_error_modal(
        self, long_name, title_word
    ):
        dialog = _make_dialog()
        with patch("pattern_manager_dialog.QMessageBox.warning") as warn:
            dialog.handle_response({
                "action": long_name,
                "data": {"success": False, "error": "handler not ready"},
            })
        warn.assert_called_once()
        assert title_word in warn.call_args[0][1]

    def test_banner_accessible_name_and_description(self):
        # Consistent with the stage-6 accessibility sweep.
        dialog = _make_dialog()
        assert dialog._banner_label.accessibleName() != ""
        assert dialog._banner_label.accessibleDescription() != ""


class TestEditorDialogCleanup:
    """_open_editor must not leak a full CreatePatternDialog widget tree
    per Add/Edit/Duplicate/Customize (the manager never closes --
    closeEvent hides), and the editor's timers must not fire once after
    close and send a stray pm_test_draft (wh-pattern-editor-r0.6)."""

    def test_open_editor_stops_timers_and_schedules_deletion(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        from create_pattern_dialog import CreatePatternDialog

        manager = _make_dialog()
        captured = {}

        def fake_exec(dialog_self):
            captured["dialog"] = dialog_self
            captured["children_during"] = manager.findChildren(
                CreatePatternDialog
            )
            # Simulate closing mid-activity: the try-it debounce and the
            # save watchdog are both running when exec() returns.
            dialog_self._try_input.setText("deploy")
            assert dialog_self._try_timer.isActive()
            dialog_self._save_timeout_timer.start()
            return 0

        with patch.object(CreatePatternDialog, "exec", fake_exec):
            manager._open_editor()

        dialog = captured["dialog"]
        assert captured["children_during"] == [dialog]
        assert not dialog._try_timer.isActive()
        assert not dialog._save_timeout_timer.isActive()
        # deleteLater() was called: processing the deferred-delete events
        # destroys the dialog, so the manager holds no editor children.
        QCoreApplication.sendPostedEvents(
            None, QEvent.Type.DeferredDelete
        )
        assert manager.findChildren(CreatePatternDialog) == []


class TestManagerMinimumSize:
    def test_detail_buttons_fit_within_right_pane_at_minimum(self):
        # 800px minimum: left pane takes ~260, leaving ~520 for the detail
        # panel. The widest visible button sets (built-in selected; user
        # override selected) must fit without clipping.
        dialog = _make_dialog()
        dialog.populate(_sample_data())
        for pattern_id in ("sysid", "uid-save"):
            _select_pattern(dialog, pattern_id)
            visible = [
                b for b in (
                    dialog._edit_btn,
                    dialog._duplicate_btn,
                    dialog._customize_btn,
                    dialog._remove_custom_btn,
                    dialog._explain_btn,
                ) if not b.isHidden()
            ]
            row_width = sum(b.sizeHint().width() for b in visible)
            row_width += 6 * (len(visible) - 1)  # default layout spacing
            assert row_width <= 500, (
                f"{pattern_id}: detail buttons need {row_width}px, more "
                f"than the right pane offers at the 800px minimum"
            )
