"""Font-size tests for the pattern editor child dialog.

The Pattern Manager applies its Ctrl+=/-/0 zoom size to the editor with
``dialog.setFont(self.font())`` just before ``exec()``
(wh-pattern-font-size). Qt's font cascade carries that to the editor's
plain widgets, but not to a widget whose style sheet has been polished
(the resolved font is pinned) nor to one carrying its own explicit
``QFont``. These tests pin the widgets in both classes to the inherited
size (wh-pattern-manager-improve.1.2).

Zooming WHILE the editor is open is unreachable -- ``exec()`` is
application-modal, so the manager cannot receive a shortcut until the
editor closes -- so ``setFont`` before ``exec()`` is the only propagation
path and one application at show time is enough.
"""
from __future__ import annotations

import pytest
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QLabel

pytestmark = pytest.mark.usefixtures("qapp")

# A size far from the app default, so a widget that ignored the cascade
# cannot coincidentally match.
_ZOOMED_POINT_SIZE = 24

# Every status label the editor styles, by attribute name. Enumerated
# here rather than imported so dropping one from the production sweep
# fails this file instead of shrinking it.
_STATUS_LABEL_ATTRS = (
    "_save_error_label",
    "_try_result_label",
    "_phrase_error_label",
    "_param_error_label",
    "_record_error_label",
    "_expression_error_label",
    "_group_count_label",
    "_type_error_label",
    "_steps_error_label",
)


def _open_editor_at(point_size: int = _ZOOMED_POINT_SIZE, **kwargs):
    """Build the editor the way ``_open_editor`` does: construct, apply
    the manager's font, then show. ``showEvent`` is delivered directly
    rather than through a real ``show()`` so no native window is created
    (incidental native widgets are access-violation surface in full-suite
    runs -- see the ``mock_editor_window`` fixture note in conftest)."""
    from create_pattern_dialog import CreatePatternDialog

    dialog = CreatePatternDialog("x-ray", parent=None, **kwargs)
    font = dialog.font()
    font.setPointSize(point_size)
    dialog.setFont(font)
    dialog.showEvent(QShowEvent())
    return dialog


@pytest.mark.parametrize("attr", _STATUS_LABEL_ATTRS)
def test_status_label_follows_the_managers_font(attr):
    dialog = _open_editor_at()
    assert getattr(dialog, attr).font().pointSize() == _ZOOMED_POINT_SIZE


def test_every_styled_label_follows_the_managers_font():
    # Catches status labels held only as locals (the hotkey-page help
    # line, the goal-page hint) that no attribute test can reach.
    from create_pattern_dialog import (
        _ERROR_STYLE,
        _MUTED_STYLE,
        _OK_STYLE,
    )
    dialog = _open_editor_at()
    styles = (_ERROR_STYLE, _MUTED_STYLE, _OK_STYLE)
    styled = [
        label for label in dialog.findChildren(QLabel)
        if label.styleSheet() in styles
    ]
    assert len(styled) >= len(_STATUS_LABEL_ATTRS)
    for label in styled:
        assert label.font().pointSize() == _ZOOMED_POINT_SIZE


def test_status_styles_pin_no_font_size():
    # A px font-size in the style sheet beats the widget's own font, so a
    # pinned size would silently undo every setFont above.
    from create_pattern_dialog import (
        _ERROR_STYLE,
        _MUTED_STYLE,
        _OK_STYLE,
    )
    for style in (_ERROR_STYLE, _MUTED_STYLE, _OK_STYLE):
        assert "font-size" not in style


def test_try_result_keeps_the_size_after_a_restyle():
    # _set_try_result re-applies a style sheet on every render.
    from create_pattern_dialog import _ERROR_STYLE
    dialog = _open_editor_at()
    dialog._set_try_result("No pattern responds to that", _ERROR_STYLE)
    assert dialog._try_result_label.font().pointSize() == _ZOOMED_POINT_SIZE


def test_goal_heading_scales_with_the_managers_font():
    from create_pattern_dialog import _HEADING_FONT_SIZE_OFFSET
    dialog = _open_editor_at()
    heading_font = dialog._goal_heading.font()
    assert (
        heading_font.pointSize()
        == _ZOOMED_POINT_SIZE + _HEADING_FONT_SIZE_OFFSET
    )
    assert heading_font.bold()


def test_a_step_row_added_after_open_uses_the_managers_font():
    dialog = _open_editor_at()
    dialog._steps_editor.add_step()
    row = dialog._steps_editor._rows[-1]
    assert row._summary_label.font().pointSize() == _ZOOMED_POINT_SIZE
