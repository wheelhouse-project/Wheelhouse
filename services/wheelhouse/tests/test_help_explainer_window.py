"""Tests for the HelpExplainerWindow (wh-assistant-button-explainer).

The window appears when the user chooses Help, before the browser opens.
It explains that the Wheelhouse Assistant runs inside ChatGPT, that a
ChatGPT account is required, and what signing up involves. An Assistant
button opens the assistant; a Cancel button opens nothing.

The window carries no settings and no message to another process. It
reports the user's choice with a Qt signal, and the GUI manager decides
what to send. Tests cover what a future change could break:

  * every string David approved, word for word (criterion W1);
  * both buttons, the default button, and the Escape key (W2);
  * the check box state travelling with the Assistant choice (W3);
  * the window staying modeless, so the GUI queue keeps running (W5).

The body wording is quoted from David's approval on the bead
(comment of 2026-09-20 00:56). Assertions are exact, not substrings:
the words are the deliverable, so a silent edit must fail a test.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from help_explainer_window import (
    EXPLAINER_BODY_PARAGRAPHS,
    EXPLAINER_TITLE,
    HelpExplainerWindow,
)

APPROVED_PARAGRAPHS = (
    "The Wheelhouse Assistant answers questions about Wheelhouse."
    " It runs inside ChatGPT, in your web browser.",
    "You need a ChatGPT account. A free account works.",
    'No account yet? On the ChatGPT page, select "Sign up". Enter an'
    " email address, or use a Google, Microsoft, or Apple account."
    " The sign-up takes about two minutes.",
    "Then type or dictate your question in plain words.",
)


@pytest.fixture
def window(qapp):
    win = HelpExplainerWindow()
    try:
        yield win
    finally:
        win.close()
        win.deleteLater()


class TestApprovedText:
    """W1: the title, the four paragraphs, and the check box label."""

    def test_title_is_the_approved_title(self, window):
        assert EXPLAINER_TITLE == "Ask the Wheelhouse Assistant"
        assert window.windowTitle() == "Ask the Wheelhouse Assistant"

    def test_the_four_paragraphs_are_the_approved_words(self, window):
        assert tuple(EXPLAINER_BODY_PARAGRAPHS) == APPROVED_PARAGRAPHS

    def test_the_body_shows_all_four_paragraphs(self, window):
        body = window._body_label.text()
        for paragraph in APPROVED_PARAGRAPHS:
            assert paragraph in body

    def test_the_check_box_says_do_not_show_this_again(self, window):
        assert window._do_not_show_again.text() == "Do not show this again"

    def test_the_check_box_starts_clear(self, window):
        assert window._do_not_show_again.isChecked() is False


class TestButtons:
    """W2: both buttons, their accessible names, and the default button."""

    def test_the_buttons_are_named_assistant_and_cancel(self, window):
        assert window._assistant_button.text() == "Assistant"
        assert window._cancel_button.text() == "Cancel"

    def test_the_visible_text_is_the_accessible_name(self, window):
        # Click-by-voice reads the accessible name. Qt falls back to the
        # button text when no name is set, so the name the voice user
        # says is the accessible name when there is one and the visible
        # text otherwise.
        for button, label in (
            (window._assistant_button, "Assistant"),
            (window._cancel_button, "Cancel"),
        ):
            assert (button.accessibleName() or button.text()) == label

    def test_assistant_is_the_default_button(self, window):
        # The Enter key activates the default button, so this is the
        # whole of criterion W2's Enter-key requirement.
        assert window._assistant_button.isDefault() is True
        assert window._cancel_button.isDefault() is False


class TestTheChoice:
    """W2 and W3: what the window reports, and what it does not."""

    def test_assistant_reports_the_choice_with_the_box_clear(self, window):
        seen = []
        window.assistant_chosen.connect(seen.append)
        window._assistant_button.click()
        assert seen == [False]

    def test_assistant_reports_the_checked_box(self, window):
        seen = []
        window.assistant_chosen.connect(seen.append)
        window._do_not_show_again.setChecked(True)
        window._assistant_button.click()
        assert seen == [True]

    def test_assistant_closes_the_window(self, window):
        window.show()
        window._assistant_button.click()
        assert window.isVisible() is False

    def test_cancel_reports_nothing_and_closes(self, window):
        seen = []
        window.assistant_chosen.connect(seen.append)
        window.show()
        window._cancel_button.click()
        assert seen == []
        assert window.isVisible() is False

    def test_cancel_reports_nothing_even_when_the_box_is_checked(self, window):
        # W3: "Cancel saves nothing, whatever the box says."
        seen = []
        window.assistant_chosen.connect(seen.append)
        window._do_not_show_again.setChecked(True)
        window.show()
        window._cancel_button.click()
        assert seen == []
        assert window.isVisible() is False

    def test_escape_reports_nothing_and_closes(self, window):
        seen = []
        window.assistant_chosen.connect(seen.append)
        window.show()
        window.reject()  # what the Escape key calls on a QDialog
        assert seen == []
        assert window.isVisible() is False


class TestModeless:
    """W5: the GUI queue keeps being processed while the window is open."""

    def test_the_window_is_not_modal(self, window):
        assert window.isModal() is False
        assert window.windowModality() == Qt.WindowModality.NonModal

    def test_the_box_clears_again_for_a_second_showing(self, window):
        # The GUI keeps one instance and raises it (criterion W5), so a
        # box left checked from an earlier showing must not travel with
        # a later Assistant press.
        window._do_not_show_again.setChecked(True)
        window.reject()
        window.prepare_to_show()
        assert window._do_not_show_again.isChecked() is False
