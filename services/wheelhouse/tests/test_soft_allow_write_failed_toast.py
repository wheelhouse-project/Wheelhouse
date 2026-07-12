"""Tests for the SoftAllowWriteFailedToast widget (wh-9dkse).

The disk-write-fails follow-up toast is a separate widget from the
rejection toast and the three-strikes grant prompt. It surfaces a
single-button acknowledgment when the soft-allow file could not be
persisted on disk:

  Title: "WheelHouse couldn't save your choice"
  Body:  "Try saying the words again later, then click Yes again."
  Buttons: [OK]

The widget knows nothing about the identity tuple that failed to
persist; the GUI manager composes the strings and passes them in.
There is no retry path in the widget -- the user re-attempts later.

Coverage:
  * The widget builds with title, body, and OK button.
  * ``show_message`` populates title and body strings.
  * Clicking OK closes the toast (no payload signal -- the toast is
    informational only).
  * The dismiss X also closes the toast.
  * Auto-dismiss closes the toast after the configured lifetime.
  * A second ``show_message`` resets the lifetime timer so a long
    second presentation is not cut short by the first call's timer.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def toast(qtbot):
    from soft_allow_write_failed_toast import SoftAllowWriteFailedToast
    widget = SoftAllowWriteFailedToast()
    qtbot.addWidget(widget)
    yield widget
    widget.close()


class TestWidgetConstruction:
    def test_widget_starts_hidden(self, toast):
        assert not toast.isVisible()

    def test_has_title_and_body_labels(self, toast):
        assert toast._title_label is not None
        assert toast._body_label is not None

    def test_has_ok_button(self, toast):
        assert toast._ok_button is not None
        assert toast._ok_button.text() == "OK"

    def test_has_dismiss_button(self, toast):
        assert toast._dismiss_button is not None
        assert toast._dismiss_button.text() == "X"


class TestShowMessage:
    def test_show_message_sets_title_and_body(self, toast):
        toast.show_message(
            title="WheelHouse couldn't save your choice",
            body="Try saying the words again later, then click Yes again.",
        )
        assert toast.isVisible()
        assert toast._title_label.text() == (
            "WheelHouse couldn't save your choice"
        )
        assert toast._body_label.text() == (
            "Try saying the words again later, then click Yes again."
        )

    def test_show_message_re_enables_ok_button(self, toast):
        toast.show_message(title="t", body="b")
        toast._ok_button.setEnabled(False)

        toast.show_message(title="t2", body="b2")

        assert toast._ok_button.isEnabled()


class TestOkClick:
    def test_ok_click_closes_toast(self, toast):
        toast.show_message(title="t", body="b")
        assert toast.isVisible()
        toast._ok_button.click()
        assert not toast.isVisible()

    def test_ok_click_disables_button_to_block_double_fire(self, toast):
        toast.show_message(title="t", body="b")
        toast._ok_button.click()
        # The button is disabled before close so a second click on
        # the same dialog is a noop. Mirrors the grant-prompt and
        # rejection-toast double-click guards.
        assert not toast._ok_button.isEnabled()


class TestDismiss:
    def test_dismiss_hides_widget(self, toast):
        toast.show_message(title="t", body="b")
        assert toast.isVisible()
        toast._dismiss_button.click()
        assert not toast.isVisible()


class TestAutoDismiss:
    def test_auto_dismiss_hides_widget(self, toast, qtbot):
        toast.show_message(title="t", body="b", lifetime_ms=500)
        qtbot.waitUntil(lambda: not toast.isVisible(), timeout=2000)


class TestLifetimeTimerReset:
    """A second show_message with a longer lifetime must override
    the first call's short timer, mirroring the equivalent guard
    on RejectionToast and GrantPromptToast (wh-vbvgf.15.1)."""

    def test_re_show_resets_lifetime_timer(self, toast, qtbot):
        toast.show_message(title="t", body="b", lifetime_ms=500)
        toast.show_message(title="t2", body="b2", lifetime_ms=2000)
        # After 800ms the toast is still visible because the second
        # show set a fresh 2000ms lifetime.
        qtbot.wait(800)
        assert toast.isVisible()

    def test_lifetime_clamped_to_minimum(self, toast):
        # The widget enforces a 500ms minimum so a caller cannot
        # accidentally set a non-positive lifetime that would never
        # auto-dismiss. Direct assert on the timer interval mirrors
        # the equivalent guard on RejectionToast (wh-vbvgf.15.2).
        toast.show_message(title="t", body="b", lifetime_ms=0)
        assert toast._lifetime_timer.interval() >= 500
