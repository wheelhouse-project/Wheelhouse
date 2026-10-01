"""Screen-fit and scrolling tests for the pattern editor dialog
(wh-pattern-editor-steps-scroll).

David's Edit Pattern dialog for a four-step pattern ran past the bottom of
the screen: the lower steps' fields and the Save / Cancel buttons could not
be reached, because ``CreatePatternDialog._build_ui`` put everything in
plain layouts and a window grows to whatever its layouts ask for. The
editor page now keeps everything above the Save / Cancel row in a scroll
area, and the dialog is bounded to the available area of the monitor it
opens on.

Every test fakes the screen's available rectangle instead of trusting the
offscreen platform's screen size, so the results do not depend on the
machine. The seam is the codebase's established one (see
``test_pattern_manager_window_scaling.py``): a Python attribute on the
class shadows the inherited Qt ``screen()`` binding, because the dialog
calls ``self.screen()`` from Python.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

pytestmark = pytest.mark.usefixtures("qapp")

# An available area far smaller than any multi-step editor, so the steps
# cannot fit, at a non-zero origin so a dialog placed at 0,0 is caught.
SMALL = QRect(100, 50, 800, 400)
# A normal desktop work area (1080p minus the taskbar).
NORMAL = QRect(0, 0, 1920, 1040)
# Roomy enough for a one-step editor, small enough that many steps overflow.
MID = QRect(0, 0, 1000, 700)


class _FakeScreen:
    def __init__(self, rect):
        self._rect = rect

    def availableGeometry(self):
        return QRect(self._rect)


def _fake_screen(monkeypatch, rect):
    from create_pattern_dialog import CreatePatternDialog

    monkeypatch.setattr(
        CreatePatternDialog, "screen", lambda self: _FakeScreen(rect)
    )


@pytest.fixture
def opener(monkeypatch):
    """Return ``open_dialog(rect, **kwargs)``; every dialog is closed after."""
    opened = []

    def open_dialog(rect, parent=None, **kwargs):
        from create_pattern_dialog import CreatePatternDialog

        _fake_screen(monkeypatch, rect)
        dialog = CreatePatternDialog("x-ray", parent=parent, **kwargs)
        opened.append(dialog)
        dialog.show()
        _settle()
        return dialog

    yield open_dialog
    for dialog in opened:
        dialog.close()
        dialog.deleteLater()
    _settle()


def _steps(count, function="type_text"):
    return [
        {"function": function, "params": [f"text {i}"]} for i in range(count)
    ]


def _entry(steps, **overrides):
    entry = {
        "id": "c" * 64,
        "trigger_display": "deploy",
        "requires_hotword": True,
        "is_user_created": True,
        "overrides_builtin": False,
        "raw_pattern": r"^(?:deploy|ship\ it)$",
        "raw_actions": steps,
        "description": "steps",
    }
    entry.update(overrides)
    return entry


def _edit_dialog(opener, rect, steps, **kwargs):
    entry = _entry(steps)
    return opener(rect, entry=entry, pattern_id=entry["id"], **kwargs)


def _first_field(row):
    """A step row's first parameter field."""
    return row._param_widgets[0][1]


def _settle():
    """Run the event loop until posted layout and growth work is done.

    A layout change posts a layout request, whose handling starts a
    zero-delay timer, so one ``processEvents`` call is not enough.
    """
    for _ in range(6):
        QApplication.processEvents()


def _visible_in_viewport(scroll, widget):
    """True when the middle of the widget's left edge is inside the scroll
    area's viewport, so the user can see where the field starts and read
    its text line.

    The whole widget is not required: Qt's own scroll-to-focus code
    (``QScrollArea.ensureWidgetVisible``) treats a line edit as visible
    once its text cursor is in view, which can leave a few pixels of the
    field's border cut off, and a wide field stays partly off to the
    right on a screen narrower than the content's minimum width, which
    the horizontal scroll bar covers.
    """
    point = widget.mapTo(
        scroll.viewport(), QPoint(0, widget.height() // 2)
    )
    return scroll.viewport().rect().contains(point)


def _inside_screen(dialog, rect):
    return rect.contains(dialog.frameGeometry())


def _wheel_event(widget, delta=-120):
    pos = QPointF(widget.rect().center())
    return QWheelEvent(
        pos, QPointF(widget.mapToGlobal(pos.toPoint())), QPoint(0, 0),
        QPoint(0, delta), Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False,
    )


def _wheel(widget, delta=-120):
    QApplication.sendEvent(widget, _wheel_event(widget, delta))


# ---------------------------------------------------------------------------
# Item 1: the dialog never extends past the screen; the steps scroll
# ---------------------------------------------------------------------------


class TestDialogStaysOnScreen:
    def test_edit_dialog_with_many_steps_fits_the_available_area(self, opener):
        dialog = _edit_dialog(opener, SMALL, _steps(8))
        assert _inside_screen(dialog, SMALL), dialog.frameGeometry()
        assert dialog._editor_scroll.verticalScrollBar().maximum() > 0

    def test_last_step_field_can_be_reached_and_typed_into(self, opener):
        dialog = _edit_dialog(opener, SMALL, _steps(8))
        assert _inside_screen(dialog, SMALL), dialog.frameGeometry()
        scroll = dialog._editor_scroll
        field = _first_field(dialog._steps_editor._rows[-1])
        assert not _visible_in_viewport(scroll, field)
        scroll.ensureWidgetVisible(field)
        assert _visible_in_viewport(scroll, field)
        field.clear()
        QTest.keyClicks(field, "reached the end")
        emitted = []
        dialog.pattern_action.connect(emitted.append)
        dialog._save_btn.click()
        assert len(emitted) == 1
        actions = emitted[0]["data"]["data"]["actions"]
        assert len(actions) == 8
        assert actions[-1] == {
            "function": "type_text", "params": ["reached the end"],
        }

    def test_add_dialog_with_many_steps_fits_the_available_area(self, opener):
        dialog = opener(SMALL)
        dialog._root_stack.setCurrentWidget(dialog._editor_page)
        dialog._advanced_toggle.setChecked(True)
        for _ in range(7):
            dialog._steps_editor.add_step()
        _settle()
        assert len(dialog._steps_editor.steps()) == 8
        assert _inside_screen(dialog, SMALL), dialog.frameGeometry()
        assert dialog._editor_scroll.verticalScrollBar().maximum() > 0

    def test_dialog_is_bounded_by_the_parents_screen(self, opener, monkeypatch):
        class _Parent(QWidget):
            def screen(self):
                return _FakeScreen(SMALL)

        parent = _Parent()
        entry = _entry(_steps(8))
        dialog = opener(
            NORMAL, parent=parent, entry=entry, pattern_id=entry["id"]
        )
        # The dialog's own screen is the roomy one; the parent's is small.
        assert _inside_screen(dialog, SMALL), dialog.frameGeometry()
        parent.deleteLater()

    def test_window_frame_is_counted_in_the_bound(self, opener, monkeypatch):
        from create_pattern_dialog import CreatePatternDialog

        monkeypatch.setattr(
            CreatePatternDialog, "frameGeometry",
            lambda self: self.geometry().adjusted(-8, -31, 8, 8),
        )
        dialog = _edit_dialog(opener, SMALL, _steps(8))
        assert SMALL.contains(dialog.frameGeometry()), dialog.frameGeometry()

    def test_showing_the_dialog_runs_the_screen_fit_helper(
        self, monkeypatch
    ):
        from create_pattern_dialog import CreatePatternDialog

        calls = []
        monkeypatch.setattr(
            CreatePatternDialog, "_fit_to_screen",
            lambda self: calls.append(self),
        )
        dialog = CreatePatternDialog("x-ray", parent=None)
        try:
            dialog.show()
            _settle()
            assert calls == [dialog]
        finally:
            dialog.close()


class TestNaturalSizeIsKept:
    @pytest.mark.parametrize("kind", ["simple", "advanced"])
    def test_one_step_pattern_needs_no_scroll_bar(self, opener, kind):
        if kind == "simple":
            entry = _entry(
                [{"function": "hk", "params": ["ctrl", "d"]}],
                phrases=["deploy", "ship it"],
            )
        else:
            entry = _entry(_steps(1))
        dialog = opener(NORMAL, entry=entry, pattern_id=entry["id"])
        assert dialog._editor_scroll.verticalScrollBar().maximum() == 0
        assert not dialog._editor_scroll.verticalScrollBar().isVisible()
        assert _inside_screen(dialog, NORMAL)


# ---------------------------------------------------------------------------
# Item 1: mouse wheel
# ---------------------------------------------------------------------------


class TestWheel:
    def test_wheel_over_the_scroll_area_scrolls_it(self, opener):
        dialog = _edit_dialog(opener, SMALL, _steps(8))
        scroll = dialog._editor_scroll
        bar = scroll.verticalScrollBar()
        assert bar.maximum() > 0
        before = bar.value()
        _wheel(scroll.viewport())
        assert bar.value() > before

    @pytest.mark.parametrize("which", ["function", "choice", "group_ref"])
    def test_wheel_over_an_unfocused_combo_scrolls_instead(
        self, opener, which
    ):
        steps = _steps(8)
        if which == "choice":
            steps[0] = {"function": "scroll", "params": ["down"]}
        elif which == "group_ref":
            steps[0] = {"function": "literal", "params": ["g1"]}
        entry = _entry(steps, raw_pattern=r"^say (.+)$")
        dialog = opener(SMALL, entry=entry, pattern_id=entry["id"])
        row = dialog._steps_editor._rows[0]
        combo = (
            row._function_combo if which == "function"
            else row._param_widgets[0][1]
        )
        combo.clearFocus()
        assert not combo.hasFocus()
        index_before = combo.currentIndex()
        text_before = combo.currentText()
        # Qt hands a wheel event a combo does not accept to its parent
        # widgets, up to the scroll area's viewport, only for events that
        # come from the window system; a synthetic one is not passed on. So
        # the combo's own handling is checked here (selection unchanged,
        # event left unaccepted) and the viewport's in the test above.
        event = _wheel_event(combo, -120)
        combo.wheelEvent(event)
        assert combo.currentIndex() == index_before
        assert combo.currentText() == text_before
        assert row.step() == steps[0]
        assert not event.isAccepted()

    def test_combos_take_focus_by_click_or_tab_only(self, opener):
        dialog = _edit_dialog(opener, SMALL, _steps(2))
        combo = dialog._steps_editor._rows[0]._function_combo
        assert combo.focusPolicy() == Qt.FocusPolicy.StrongFocus

    def test_wheel_over_a_focused_combo_still_changes_it(self, opener):
        dialog = _edit_dialog(opener, SMALL, _steps(2))
        dialog.activateWindow()
        QTest.qWaitForWindowActive(dialog)
        combo = dialog._steps_editor._rows[0]._function_combo
        combo.setFocus()
        assert combo.hasFocus()
        before = combo.currentIndex()
        _wheel(combo, -120)
        assert combo.currentIndex() != before


# ---------------------------------------------------------------------------
# Item 1: keyboard
# ---------------------------------------------------------------------------


class TestKeyboard:
    def test_tabbing_to_a_field_below_the_fold_scrolls_it_into_view(
        self, opener
    ):
        dialog = _edit_dialog(opener, SMALL, _steps(8))
        assert _inside_screen(dialog, SMALL), dialog.frameGeometry()
        dialog.activateWindow()
        QTest.qWaitForWindowActive(dialog)
        scroll = dialog._editor_scroll
        bar = scroll.verticalScrollBar()
        assert bar.maximum() > 0
        target = _first_field(dialog._steps_editor._rows[-1])
        assert not _visible_in_viewport(scroll, target)
        dialog._steps_editor._rows[0]._function_combo.setFocus()
        for _ in range(120):
            focus = dialog.focusWidget()
            assert focus is not None
            QTest.keyClick(focus, Qt.Key.Key_Tab)
            focus = dialog.focusWidget()
            if focus is target:
                break
        _settle()
        assert dialog.focusWidget() is target
        assert bar.value() > 0
        assert _visible_in_viewport(scroll, target)

    def test_the_scroll_area_never_takes_keyboard_focus(self, opener):
        # A stock QScrollArea accepts Tab focus. The editor then opened
        # with focus on an unnamed control instead of the expression
        # field, and every Tab cycle had an extra unnamed stop after
        # Cancel -- a screen reader announces nothing there.
        dialog = _edit_dialog(opener, SMALL, _steps(8))
        dialog.activateWindow()
        QTest.qWaitForWindowActive(dialog)
        _settle()
        assert dialog.focusWidget() is dialog._expression_edit
        seen = []
        for _ in range(200):
            focus = dialog.focusWidget()
            assert focus is not None
            seen.append(focus)
            QTest.keyClick(focus, Qt.Key.Key_Tab)
        assert dialog._cancel_btn in seen
        assert dialog._editor_scroll not in seen


# ---------------------------------------------------------------------------
# Item 2: Save and Cancel stay in view
# ---------------------------------------------------------------------------


class TestButtonsStayVisible:
    def test_save_and_cancel_sit_outside_the_scroll_area_and_on_screen(
        self, opener
    ):
        dialog = _edit_dialog(opener, SMALL, _steps(8))
        scroll = dialog._editor_scroll
        assert dialog._editor_scroll.verticalScrollBar().maximum() > 0
        for button in (dialog._save_btn, dialog._cancel_btn):
            assert not scroll.isAncestorOf(button)
            assert button.isVisible()
            top_left = button.mapTo(dialog, QPoint(0, 0))
            assert dialog.rect().contains(QRect(top_left, button.size()))
            global_rect = QRect(button.mapToGlobal(QPoint(0, 0)), button.size())
            assert SMALL.contains(global_rect), global_rect
        # Scrolling the steps to the bottom moves neither button.
        before = dialog._save_btn.mapTo(dialog, QPoint(0, 0))
        bar = scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        assert dialog._save_btn.mapTo(dialog, QPoint(0, 0)) == before


# ---------------------------------------------------------------------------
# Item 3: four steps create, save, reopen, edit
# ---------------------------------------------------------------------------


class TestFourStepPatternRoundTrip:
    def test_four_step_pattern_is_created_reopened_and_edited(self, opener):
        # Create through the dialog, on a screen too small to show it whole.
        dialog = opener(SMALL)
        dialog._root_stack.setCurrentWidget(dialog._editor_page)
        dialog._phrase_editor.set_phrases(["four step demo"])
        dialog._advanced_toggle.setChecked(True)
        for _ in range(3):
            dialog._steps_editor.add_step()
        _settle()
        assert _inside_screen(dialog, SMALL), dialog.frameGeometry()
        scroll = dialog._editor_scroll
        rows = dialog._steps_editor._rows
        assert len(rows) == 4
        for i, row in enumerate(rows):
            combo = row._function_combo
            combo.setCurrentIndex(combo.findData("type_text"))
            _settle()
            field = _first_field(row)
            scroll.ensureWidgetVisible(field)
            assert _visible_in_viewport(scroll, field)
            field.clear()
            QTest.keyClicks(field, f"step {i}")
        assert dialog._save_btn.isEnabled()
        save_rect = QRect(
            dialog._save_btn.mapToGlobal(QPoint(0, 0)),
            dialog._save_btn.size(),
        )
        assert SMALL.contains(save_rect), save_rect
        created = []
        dialog.pattern_action.connect(created.append)
        dialog._save_btn.click()
        assert len(created) == 1
        assert created[0]["action"] == "pm_create_pattern"
        saved = created[0]["data"]
        expected = [
            {"function": "type_text", "params": [f"step {i}"]}
            for i in range(4)
        ]
        assert saved["actions"] == expected

        # Reopen from the saved data and change one step.
        entry = _entry(
            saved["actions"], raw_pattern=saved["expression"],
            id="d" * 64,
        )
        reopened = opener(SMALL, entry=entry, pattern_id=entry["id"])
        assert reopened.mode == "advanced"
        assert len(reopened._steps_editor._rows) == 4
        assert _inside_screen(reopened, SMALL), reopened.frameGeometry()
        scroll = reopened._editor_scroll
        field = _first_field(reopened._steps_editor._rows[3])
        scroll.ensureWidgetVisible(field)
        assert _visible_in_viewport(scroll, field)
        field.clear()
        QTest.keyClicks(field, "changed")
        updated = []
        reopened.pattern_action.connect(updated.append)
        reopened._save_btn.click()
        assert len(updated) == 1
        assert updated[0]["action"] == "pm_update_pattern"
        assert updated[0]["data"]["pattern_id"] == entry["id"]
        actions = updated[0]["data"]["data"]["actions"]
        assert actions == expected[:3] + [
            {"function": "type_text", "params": ["changed"]},
        ]


# ---------------------------------------------------------------------------
# Growth after show
# ---------------------------------------------------------------------------


class TestGrowthAfterShow:
    def test_adding_steps_grows_the_dialog_but_never_past_the_screen(
        self, opener
    ):
        dialog = _edit_dialog(opener, MID, _steps(1))
        start = dialog.height()
        dialog._steps_editor.add_step()
        _settle()
        after_one = dialog.height()
        assert after_one > start
        for _ in range(8):
            dialog._steps_editor.add_step()
        _settle()
        assert dialog.height() >= after_one
        assert _inside_screen(dialog, MID), dialog.frameGeometry()
        assert dialog._editor_scroll.verticalScrollBar().maximum() > 0

    def test_removing_steps_never_shrinks_the_dialog(self, opener):
        dialog = _edit_dialog(opener, MID, _steps(6))
        height = dialog.height()
        for _ in range(4):
            dialog._steps_editor.remove_step(0)
        _settle()
        assert dialog.height() == height

    def test_adding_phrase_rows_grows_the_dialog_but_never_past_the_screen(
        self, opener
    ):
        entry = _entry(
            [{"function": "hk", "params": ["ctrl", "d"]}],
            phrases=["deploy", "ship it"],
        )
        dialog = opener(MID, entry=entry, pattern_id=entry["id"])
        assert dialog.mode == "simple"
        start = dialog.height()
        dialog._phrase_editor.add_row("one more")
        _settle()
        assert dialog.height() > start
        for i in range(30):
            dialog._phrase_editor.add_row(f"phrase {i}")
        _settle()
        assert _inside_screen(dialog, MID), dialog.frameGeometry()
        assert dialog._editor_scroll.verticalScrollBar().maximum() > 0

    def test_switching_to_advanced_needs_no_scroll_bar_on_a_roomy_screen(
        self, opener
    ):
        # Both panes share one stacked area sized for the taller of the two,
        # so the switch itself never asks for more room.
        entry = _entry(
            [{"function": "hk", "params": ["ctrl", "d"]}],
            phrases=["deploy", "ship it"],
        )
        dialog = opener(NORMAL, entry=entry, pattern_id=entry["id"])
        dialog._advanced_toggle.setChecked(True)
        _settle()
        assert dialog.mode == "advanced"
        assert dialog._editor_scroll.verticalScrollBar().maximum() == 0

    def test_a_dialog_the_user_made_smaller_is_not_regrown_by_typing(
        self, opener
    ):
        dialog = _edit_dialog(opener, NORMAL, _steps(2))
        dialog.resize(dialog.width(), dialog.height() - 150)
        _settle()
        small = dialog.height()
        field = _first_field(dialog._steps_editor._rows[0])
        QTest.keyClicks(field, "x")
        _settle()
        assert dialog.height() == small


class TestAddFlow:
    def test_choosing_a_goal_gives_the_editor_its_natural_size(self, opener):
        from create_pattern_dialog import _GOAL_TEMPLATES

        dialog = opener(NORMAL)
        assert dialog._root_stack.currentWidget() is dialog._goal_page
        dialog._apply_goal_template(_GOAL_TEMPLATES[0])
        _settle()
        assert dialog._root_stack.currentWidget() is dialog._editor_page
        assert dialog._editor_scroll.verticalScrollBar().maximum() == 0
        assert not dialog._editor_scroll.horizontalScrollBar().isVisible()
        assert _inside_screen(dialog, NORMAL), dialog.frameGeometry()


# ---------------------------------------------------------------------------
# Messages stay in view (wh-pattern-editor-steps-scroll.1.1)
# ---------------------------------------------------------------------------


def _rect_in(widget, ancestor):
    """The widget's whole rectangle in the ancestor's coordinates."""
    return QRect(widget.mapTo(ancestor, QPoint(0, 0)), widget.size())


def _scroll_to(dialog, end):
    bar = dialog._editor_scroll.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.minimum() if end == "top" else bar.maximum())
    _settle()


def _assert_message_stays_in_view(dialog, label, rect):
    """The label sits outside the scroll area, above the Save row, and is
    fully inside both the dialog and the screen."""
    scroll = dialog._editor_scroll
    assert not scroll.isAncestorOf(label)
    assert label.isVisible()
    in_dialog = _rect_in(label, dialog)
    assert dialog.rect().contains(in_dialog), in_dialog
    global_rect = QRect(label.mapToGlobal(QPoint(0, 0)), label.size())
    assert rect.contains(global_rect), global_rect
    save_top = dialog._save_btn.mapTo(dialog, QPoint(0, 0)).y()
    assert in_dialog.bottom() < save_top, (in_dialog, save_top)
    # Directly above the Save row means below the scroll area as well.
    scroll_bottom = _rect_in(scroll, dialog).bottom()
    assert in_dialog.top() > scroll_bottom, (in_dialog, scroll_bottom)


class TestMessagesStayInView:
    @pytest.mark.parametrize("end", ["top", "bottom"])
    @pytest.mark.parametrize("how", ["timeout", "failure"])
    def test_save_error_is_fully_visible_at_any_scroll_position(
        self, opener, how, end
    ):
        dialog = _edit_dialog(opener, SMALL, _steps(8))
        assert _inside_screen(dialog, SMALL), dialog.frameGeometry()
        _scroll_to(dialog, end)
        if how == "timeout":
            dialog._on_save_timeout()
        else:
            dialog._save_seq = 1
            dialog.handle_response({
                "action": "pm_update_result",
                "data": {
                    "success": False, "request_id": 1,
                    "error": "Could not save the pattern",
                },
            })
        _settle()
        assert dialog._save_error_label.text()
        _assert_message_stays_in_view(dialog, dialog._save_error_label, SMALL)
        # Scrolling afterwards never moves it out of view.
        _scroll_to(dialog, "top" if end == "bottom" else "bottom")
        _assert_message_stays_in_view(dialog, dialog._save_error_label, SMALL)

    @pytest.mark.parametrize("end", ["top", "bottom"])
    def test_steps_error_is_fully_visible_at_any_scroll_position(
        self, opener, monkeypatch, end
    ):
        dialog = _edit_dialog(opener, SMALL, _steps(8))
        assert _inside_screen(dialog, SMALL), dialog.frameGeometry()
        _scroll_to(dialog, end)
        monkeypatch.setattr(
            dialog._steps_editor, "first_invalid_key_name", lambda: "nokey"
        )
        dialog._validate()
        _settle()
        assert dialog._steps_error_label.text() == "Unknown key name: 'nokey'"
        _assert_message_stays_in_view(dialog, dialog._steps_error_label, SMALL)
        _scroll_to(dialog, "top" if end == "bottom" else "bottom")
        _assert_message_stays_in_view(dialog, dialog._steps_error_label, SMALL)

    def test_steps_error_is_hidden_after_leaving_advanced_mode(self, opener):
        # The steps error belongs to the advanced pane's step list; outside
        # that pane nothing clears it, so leaving advanced mode must hide it.
        entry = _entry(
            [{"function": "hk", "params": ["ctrl", "s"]}],
            phrases=["deploy", "ship it"],
        )
        dialog = opener(NORMAL, entry=entry, pattern_id=entry["id"])
        assert dialog.mode == "simple"
        dialog._key_input.setText("nokey")
        _settle()
        dialog._advanced_toggle.setChecked(True)
        _settle()
        assert dialog._steps_error_label.isVisible()
        dialog._advanced_toggle.setChecked(False)
        _settle()
        assert not dialog._steps_error_label.isVisible()
        dialog._key_input.setText("ctrl+s")
        _settle()
        assert dialog._save_btn.isEnabled()
        assert not dialog._steps_error_label.isVisible()

    def test_try_result_line_scrolls_into_view_when_it_appears_and_changes(
        self, opener
    ):
        from create_pattern_dialog import _OK_STYLE

        dialog = _edit_dialog(opener, SMALL, _steps(8))
        assert _inside_screen(dialog, SMALL), dialog.frameGeometry()
        scroll = dialog._editor_scroll
        label = dialog._try_result_label
        viewport = scroll.viewport().rect()

        def whole_label_in_viewport():
            # Vertical extent only: the content is a few pixels wider than
            # the viewport once the vertical scroll bar takes its width
            # (the horizontal bar covers that), so the label's full width
            # never fits and is not what this test is about.
            shown = _rect_in(label, scroll.viewport())
            return (
                shown.top() >= viewport.top()
                and shown.bottom() <= viewport.bottom()
            )

        first = (
            "Saying 'x' will run this pattern\ng1 captured: 'a'\n"
            "Steps: type_text(text 0)"
        )
        second = (
            "Saying 'y' will run this pattern\ng1 captured: 'b'\n"
            "g2 captured nothing\nSteps: type_text(text 0)"
        )
        dialog._try_input.setFocus()
        _settle()
        assert dialog.focusWidget() is dialog._try_input
        _scroll_to(dialog, "top")
        assert not whole_label_in_viewport()
        dialog._set_try_result(first, _OK_STYLE)
        _settle()
        assert label.text()
        assert whole_label_in_viewport(), _rect_in(label, scroll.viewport())

        # A change of text scrolls it back into view as well.
        _scroll_to(dialog, "top")
        assert not whole_label_in_viewport()
        dialog._set_try_result(second, _OK_STYLE)
        _settle()
        assert whole_label_in_viewport(), _rect_in(label, scroll.viewport())

    def test_try_result_never_scrolls_away_from_the_field_being_edited(
        self, opener
    ):
        """A result re-run by a step edit, or one whose text did not
        change, leaves the scroll position alone (Boss e8 ruling on
        wh-pattern-editor-steps-scroll.1.1 item 2)."""
        from create_pattern_dialog import _OK_STYLE

        dialog = _edit_dialog(opener, SMALL, _steps(8))
        bar = dialog._editor_scroll.verticalScrollBar()

        # Focus in the first step's field, and the text changes.
        field = _first_field(dialog._steps_editor._rows[0])
        field.setFocus()
        _settle()
        assert dialog.focusWidget() is field
        _scroll_to(dialog, "top")
        dialog._set_try_result("Saying 'x' will run this pattern", _OK_STYLE)
        _settle()
        assert bar.value() == bar.minimum()
        dialog._set_try_result("No pattern matches 'x'", _OK_STYLE)
        _settle()
        assert bar.value() == bar.minimum()

        # Focus in the try-it input, and the text is the same as before.
        dialog._try_input.setFocus()
        _settle()
        _scroll_to(dialog, "top")
        dialog._set_try_result("No pattern matches 'x'", _OK_STYLE)
        _settle()
        assert bar.value() == bar.minimum()


# ---------------------------------------------------------------------------
# Growth after show uses the dialog's own monitor
# (wh-pattern-editor-steps-scroll.1.2)
# ---------------------------------------------------------------------------


class TestGrowthUsesTheDialogsOwnMonitor:
    def test_growth_after_show_stays_on_the_monitor_holding_the_dialog(
        self, monkeypatch
    ):
        from create_pattern_dialog import CreatePatternDialog

        first = QRect(0, 0, 1000, 480)
        second = QRect(1200, 100, 1000, 680)

        class _Parent(QWidget):
            def screen(self):
                return _FakeScreen(first)

        monkeypatch.setattr(
            CreatePatternDialog, "screen", lambda self: _FakeScreen(first)
        )
        parent = _Parent()
        entry = _entry(_steps(1))
        dialog = CreatePatternDialog(
            "x-ray", parent=parent, entry=entry, pattern_id=entry["id"]
        )
        try:
            dialog.show()
            _settle()
            # The first fit follows the parent's monitor.
            assert first.contains(dialog.frameGeometry()), dialog.frameGeometry()
            # The user drags the dialog to the second monitor.
            monkeypatch.setattr(
                CreatePatternDialog, "screen",
                lambda self: _FakeScreen(second),
            )
            dialog.move(second.x() + 20, second.y() + 20)
            _settle()
            for _ in range(10):
                dialog._steps_editor.add_step()
            _settle()
            assert second.contains(dialog.frameGeometry()), dialog.frameGeometry()
            # The bound is the second monitor's, taller than the first's.
            assert dialog.height() > first.height()
        finally:
            dialog.close()
            parent.deleteLater()
            _settle()

    def test_growth_that_changes_nothing_does_not_move_the_window(
        self, opener
    ):
        dialog = _edit_dialog(opener, SMALL, _steps(8))
        assert _inside_screen(dialog, SMALL), dialog.frameGeometry()
        # The user drags the dialog partly past the edge of the area.
        dialog.move(SMALL.x() + 60, SMALL.y() + 60)
        _settle()
        assert not _inside_screen(dialog, SMALL)
        before_pos = dialog.pos()
        before_size = dialog.size()
        dialog._steps_editor.add_step()
        _settle()
        # The dialog was already as tall as the area allows: no resize.
        assert dialog.size() == before_size
        assert dialog.pos() == before_pos
