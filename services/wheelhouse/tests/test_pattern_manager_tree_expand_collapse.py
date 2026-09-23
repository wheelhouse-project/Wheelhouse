"""Expand / collapse of a Pattern Manager category emits tree_changed.

wh-overlay-rewalk-after-filter.1.2 (GLM 5.3 round 1). Contract C1 names
"expand/collapse of a category" as a trigger beside the filter change.
Collapsing unrealizes the category's child rows exactly the way the filter's
setHidden does, so a UIA walk stops returning them and the painted overlay
badges float over rows that are gone.

populate() calls expandAll(), which fires itemExpanded once per category.
That burst must NOT reach the wire: C1 asks for one event per model change,
and populate already sends its own from its tail.

WHY THIS IS A SEPARATE FILE. These four tests were written at the end of
test_pattern_manager_dialog.py first. With them there, the seven-file overlay
selection died under ``-q`` with a 0xc0000374 heap corruption, 5 runs out of
5, inside _apply_screen_bounded_size at collected item 135
(TestRemoveCustomization::test_confirm_no_emits_nothing) -- an item that runs
long BEFORE these tests and constructs a dialog in code this branch does not
touch. Deselecting just these four made the same selection pass 5 of 5.
Disposing their dialogs did not help (4 of 5 still crashed), and commenting
out the new itemExpanded / itemCollapsed connects did not stop it either
(1 of 2 crashed), so neither the production change nor the leak is the cause.
Moving them into their own module is what cleared it. The underlying heap
corruption is older than this branch and is reported on the bead for a
ruling; this file is not a fix for it.

VISIBILITY. These tests do NOT call dialog.show(): a real top-level window on
this machine would flash on screen and could take focus from whatever the
user is doing. _emit_tree_changed reads visibility through self.isVisible(),
so the shown-stub exercises the shown path exactly. winId() needs no show --
it creates the native handle on demand.
"""

from __future__ import annotations

import pytest

from tests.test_pattern_manager_dialog import (
    _make_dialog,
    _record_tree_changed,
    _sample_data,
    _shown,
)

# wh-pytest-flaky-segfault: constructing the dialog builds real Qt widgets;
# without a QApplication Qt aborts the whole interpreter. The session-scoped
# qapp fixture guarantees one exists even when this file runs alone.
pytestmark = pytest.mark.usefixtures("qapp")


def _first_category(dialog):
    return dialog._tree.invisibleRootItem().child(0)


def test_collapsing_a_category_emits_one_tree_changed_event():
    dialog = _shown(_make_dialog())
    dialog.populate(_sample_data())
    events = _record_tree_changed(dialog)

    _first_category(dialog).setExpanded(False)

    assert len(events) == 1
    assert events[0]["action"] == "pattern_manager_tree_changed"
    assert events[0]["hwnd"] == int(dialog.window().winId())
    assert events[0]["sequence"] == 2


def test_re_expanding_a_category_emits_a_further_tree_changed_event():
    # Re-expanding brings the rows back, which changes the walk result just as
    # much as removing them did -- the same symmetry the filter tests assert.
    dialog = _shown(_make_dialog())
    dialog.populate(_sample_data())
    events = _record_tree_changed(dialog)

    category = _first_category(dialog)
    category.setExpanded(False)
    category.setExpanded(True)

    assert [e["sequence"] for e in events] == [2, 3]


def test_populate_sends_no_event_for_its_own_expand_all():
    # Two categories in _sample_data, so an unsuppressed itemExpanded connect
    # would put three events on the wire for one model change.
    dialog = _shown(_make_dialog())
    events = _record_tree_changed(dialog)

    dialog.populate(_sample_data())

    assert [e["sequence"] for e in events] == [1]
    assert dialog._tree.topLevelItemCount() == 2


def test_no_expand_collapse_event_while_the_dialog_is_not_visible():
    # Contract C6(a), the same gate the filter and populate paths use.
    dialog = _make_dialog()
    dialog.populate(_sample_data())
    assert not dialog.isVisible()
    events = _record_tree_changed(dialog)

    _first_category(dialog).setExpanded(False)

    assert events == []
