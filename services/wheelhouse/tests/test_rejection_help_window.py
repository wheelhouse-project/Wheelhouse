"""Tests for the RejectionHelpWindow (wh-b9kpc).

The help window is opened by the Why-am-I-seeing-this button on the
rejection notice. It carries no business logic and no IPC; it is a
plain modeless QDialog that explains the system to a confused user.
Tests cover only the things a future code change could break:

  * The window renders without exception in headless PySide6 mode.
  * The window carries the expected title.
  * The body text covers the four concepts the help is supposed to
    convey (Windows-tells-WheelHouse vs hidden controls, Try-it-
    anyway one-off, three-clicks-prompts-Yes-No, Yes makes it
    silent / No keeps the notice). Assertions are on substrings, not
    on exact wording, so the implementer can refine the prose
    without breaking the tests.
  * The window can be closed.
"""

from __future__ import annotations

from rejection_help_window import HELP_TITLE, RejectionHelpWindow


class TestRender:
    def test_construct_without_exception(self, qapp):
        win = RejectionHelpWindow()
        try:
            assert win is not None
        finally:
            win.deleteLater()

    def test_show_without_exception(self, qapp):
        win = RejectionHelpWindow()
        try:
            win.show()
            assert win.isVisible() is True
        finally:
            win.close()
            win.deleteLater()

    def test_window_title_is_set(self, qapp):
        win = RejectionHelpWindow()
        try:
            assert win.windowTitle() == HELP_TITLE
            assert HELP_TITLE == "Why am I seeing this?"
        finally:
            win.deleteLater()


class TestBodyText:
    """Anchor the help text to the concepts it must convey, not to
    exact wording. A future prose refinement that still covers the
    four concepts must not break the tests."""

    def test_body_explains_why_some_controls_are_easy(self, qapp):
        win = RejectionHelpWindow()
        try:
            body = win._body_label.text().lower()
            # The body needs to explain that Windows tells WheelHouse
            # for some controls and not for others. The exact wording
            # is up to the implementer, but the phrase "windows" plus
            # "text" must appear together somewhere because the user
            # needs to understand the cause.
            assert "windows" in body
            assert "text" in body
        finally:
            win.deleteLater()

    def test_body_explains_try_it_anyway_one_off(self, qapp):
        win = RejectionHelpWindow()
        try:
            body = win._body_label.text().lower()
            assert "try it anyway" in body
        finally:
            win.deleteLater()

    def test_body_explains_three_clicks_then_prompt(self, qapp):
        win = RejectionHelpWindow()
        try:
            body = win._body_label.text().lower()
            # The help must mention that three clicks trigger a
            # follow-up question. The user is confused about why
            # the notice keeps appearing and the three-click rhythm
            # is the key information.
            assert "three" in body
        finally:
            win.deleteLater()

    def test_body_explains_yes_makes_it_silent(self, qapp):
        win = RejectionHelpWindow()
        try:
            body = win._body_label.text().lower()
            # The Yes path leads to silent typing. The user needs to
            # know clicking Yes is a one-time setup that stops the
            # notice for that control type.
            assert "yes" in body
        finally:
            win.deleteLater()

    def test_body_explains_no_keeps_notice(self, qapp):
        # wh-b9kpc.1.2 (codex review): the original assertion
        # `"no" in body` matched incidental substrings like "notice"
        # and "into" and would not catch a future rewrite that
        # dropped the explicit No bullet. The strengthened test
        # anchors on real No-path concepts: the explicit "Click No"
        # cue, the "not... automatic" intent, and the "notice will
        # still appear next time" consequence.
        win = RejectionHelpWindow()
        try:
            body = win._body_label.text().lower()
            assert "click no" in body
            assert "automatic" in body
            assert "notice will still appear next time" in body
        finally:
            win.deleteLater()


class TestClose:
    def test_close_hides_window(self, qapp):
        win = RejectionHelpWindow()
        try:
            win.show()
            assert win.isVisible() is True
            win.close()
            assert win.isVisible() is False
        finally:
            win.deleteLater()


class TestScrollableBody:
    """wh-b9kpc.1.1 (codex review): the body must be inside a scroll
    area so the Close button stays reachable when the window is
    clamped to a screen smaller than the natural body height (high-
    DPI / accessibility scaling). The Close button must be outside
    the scroll area so it never scrolls out of view itself."""

    def test_body_is_inside_scroll_area(self, qapp):
        from PySide6.QtWidgets import QScrollArea

        win = RejectionHelpWindow()
        try:
            assert isinstance(win._scroll_area, QScrollArea)
            assert win._scroll_area.widget() is win._body_label
        finally:
            win.deleteLater()

    def test_close_button_is_outside_scroll_area(self, qapp):
        # The Close button must not be a descendant of the scroll
        # area, otherwise overflowing body content would push it
        # below the visible area and make it unreachable for
        # users who land in the high-DPI / large-text case.
        win = RejectionHelpWindow()
        try:
            parent = win._close_button.parent()
            while parent is not None:
                if parent is win._scroll_area:
                    raise AssertionError(
                        "Close button is inside the scroll area"
                    )
                parent = parent.parent()
        finally:
            win.deleteLater()
