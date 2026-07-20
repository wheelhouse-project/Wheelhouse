"""Tests for the GrantPromptToast widget (wh-bqv9c).

The three-strikes follow-up toast is a separate widget from the
standard rejection toast. It surfaces a Yes/No question and emits a
distinct Qt signal for each click. The widget knows nothing about
identity tuples or counters; the GUI manager attaches the per-tuple
identity when it dispatches the click signals into IPC payloads.

Coverage:
  * The widget builds with a title, body, Yes button, and No button.
  * ``show_prompt`` populates title and body strings.
  * Clicking Yes emits ``yes_clicked`` exactly once and disables both
    action buttons (so a fast double-click does not produce two
    persistence requests).
  * Clicking No emits ``no_clicked`` exactly once and disables both
    action buttons.
  * The dismiss X closes the toast without emitting either click
    signal.
  * Calling ``show_prompt`` again re-enables the buttons so the next
    presentation is fresh.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def toast(qtbot):
    from grant_prompt_toast import GrantPromptToast
    widget = GrantPromptToast()
    qtbot.addWidget(widget)
    yield widget
    widget.close()


class TestWidgetConstruction:
    def test_widget_starts_hidden(self, toast):
        assert not toast.isVisible()

    def test_has_title_and_body_labels(self, toast):
        assert toast._title_label is not None
        assert toast._body_label is not None

    def test_has_yes_and_no_buttons(self, toast):
        assert toast._yes_button is not None
        assert toast._no_button is not None
        assert toast._yes_button.text() == "Yes"
        assert toast._no_button.text() == "No"


class TestShowPrompt:
    def test_show_prompt_sets_title_and_body(self, toast):
        toast.show_prompt(
            title="Always type into Zed when you do this?",
            body="You have tried this 3 times in Zed. Wheelhouse can stop "
                 "asking and just do it from now on.",
        )
        assert toast.isVisible()
        assert toast._title_label.text() == (
            "Always type into Zed when you do this?"
        )
        assert "tried this 3 times" in toast._body_label.text()

    def test_show_prompt_re_enables_buttons(self, toast):
        toast.show_prompt(title="t", body="b")
        toast._yes_button.setEnabled(False)
        toast._no_button.setEnabled(False)

        toast.show_prompt(title="t2", body="b2")

        assert toast._yes_button.isEnabled()
        assert toast._no_button.isEnabled()


class TestYesClick:
    def test_yes_click_emits_signal(self, toast, qtbot):
        toast.show_prompt(title="t", body="b")
        with qtbot.waitSignal(toast.yes_clicked, timeout=500):
            toast._yes_button.click()

    def test_yes_click_disables_both_buttons(self, toast, qtbot):
        toast.show_prompt(title="t", body="b")
        toast._yes_button.click()
        # Both buttons disabled so a fast double-click after Yes
        # cannot also produce a No, and a second Yes cannot fire.
        assert not toast._yes_button.isEnabled()
        assert not toast._no_button.isEnabled()

    def test_yes_emits_only_once_on_double_click(self, toast, qtbot):
        toast.show_prompt(title="t", body="b")
        emit_counter = []
        toast.yes_clicked.connect(lambda: emit_counter.append(1))

        toast._yes_button.click()
        toast._yes_button.click()  # second click on disabled button is a noop

        assert emit_counter == [1]


class TestNoClick:
    def test_no_click_emits_signal(self, toast, qtbot):
        toast.show_prompt(title="t", body="b")
        with qtbot.waitSignal(toast.no_clicked, timeout=500):
            toast._no_button.click()

    def test_no_click_disables_both_buttons(self, toast, qtbot):
        toast.show_prompt(title="t", body="b")
        toast._no_button.click()
        assert not toast._no_button.isEnabled()
        assert not toast._yes_button.isEnabled()

    def test_no_emits_only_once_on_double_click(self, toast, qtbot):
        toast.show_prompt(title="t", body="b")
        emit_counter = []
        toast.no_clicked.connect(lambda: emit_counter.append(1))

        toast._no_button.click()
        toast._no_button.click()

        assert emit_counter == [1]


class TestDismiss:
    def test_dismiss_does_not_emit_yes_or_no(self, toast):
        """The dismiss X / auto-dismiss path is a separate code path from
        Yes / No. The GUI manager treats those clicks as a "dismissed
        without choosing" event and re-arms the dedup map."""

        toast.show_prompt(title="t", body="b")
        emit_yes: list = []
        emit_no: list = []
        toast.yes_clicked.connect(lambda: emit_yes.append(1))
        toast.no_clicked.connect(lambda: emit_no.append(1))

        toast._dismiss_button.click()

        assert emit_yes == []
        assert emit_no == []

    def test_dismiss_hides_widget(self, toast):
        toast.show_prompt(title="t", body="b")
        assert toast.isVisible()
        toast._dismiss_button.click()
        assert not toast.isVisible()


class TestAutoDismiss:
    """wh-6q4x coverage: auto-dismiss leaves no leaked state and
    treats the close as 'dismissed without choosing' (dismissed
    signal fires, _action_taken stays False)."""

    def test_auto_dismiss_fires_dismissed_signal(self, toast, qtbot):
        toast.show_prompt(title="t", body="b", lifetime_ms=500)
        # Wait for the lifetime timer to fire close().
        with qtbot.waitSignal(toast.dismissed, timeout=2000):
            pass
        # wh-vbvgf.14.1: assert the auto-dismiss path actually hid the
        # widget. Without this, a buggy implementation that emitted
        # `dismissed` without hiding would still pass.
        qtbot.waitUntil(lambda: not toast.isVisible(), timeout=1000)

    def test_auto_dismiss_does_not_emit_yes_or_no(self, toast, qtbot):
        toast.show_prompt(title="t", body="b", lifetime_ms=500)
        emit_yes: list = []
        emit_no: list = []
        toast.yes_clicked.connect(lambda: emit_yes.append(1))
        toast.no_clicked.connect(lambda: emit_no.append(1))
        with qtbot.waitSignal(toast.dismissed, timeout=2000):
            pass
        assert emit_yes == []
        assert emit_no == []
        # wh-vbvgf.14.1: same hide-assertion for the no-yes-no path.
        qtbot.waitUntil(lambda: not toast.isVisible(), timeout=1000)


class TestLifetimeTimerReset:
    """wh-vbvgf.15.1 (deepseek): a second show_prompt with a longer
    lifetime must override the first call's short timer. The
    RejectionToast has an equivalent test (test_re_show_resets_lifetime);
    the grant prompt did not until now."""

    def test_re_show_resets_lifetime_timer(self, toast, qtbot):
        toast.show_prompt(title="t", body="b", lifetime_ms=500)
        # Re-show with a longer lifetime before the original timer
        # would fire.
        toast.show_prompt(title="t2", body="b2", lifetime_ms=2000)
        # After 800ms the toast is still visible because the second
        # show set a fresh 2000ms lifetime; the original 500ms timer
        # was reset by QTimer.start().
        qtbot.wait(800)
        assert toast.isVisible()


class TestStateResetAcrossShows:
    """wh-6q4x coverage: button enabled / _action_taken state must
    reset on every show_prompt call so a previous Yes/No or
    auto-dismiss does not leak into the next presentation."""

    def test_action_taken_resets_to_false_on_show(self, toast):
        toast.show_prompt(title="t", body="b")
        toast._yes_button.click()
        assert toast._action_taken is True

        toast.show_prompt(title="t2", body="b2")
        assert toast._action_taken is False

    def test_buttons_re_enable_after_post_click_reshow(self, toast):
        toast.show_prompt(title="t", body="b")
        toast._yes_button.click()
        assert not toast._yes_button.isEnabled()
        assert not toast._no_button.isEnabled()

        toast.show_prompt(title="t2", body="b2")
        assert toast._yes_button.isEnabled()
        assert toast._no_button.isEnabled()


class TestDismissedSignal:
    """A close that did not come from Yes or No fires the
    ``dismissed`` signal so the GUI manager can keep the dedup map
    open for the next RetryThresholdReached event on the same tuple."""

    def test_close_without_click_emits_dismissed(self, toast, qtbot):
        toast.show_prompt(title="t", body="b")
        with qtbot.waitSignal(toast.dismissed, timeout=500):
            toast.close()

    def test_yes_click_does_not_emit_dismissed(self, toast, qtbot):
        toast.show_prompt(title="t", body="b")
        emits: list = []
        toast.dismissed.connect(lambda: emits.append(1))
        toast._yes_button.click()
        toast.close()
        assert emits == []

    def test_no_click_does_not_emit_dismissed(self, toast, qtbot):
        toast.show_prompt(title="t", body="b")
        emits: list = []
        toast.dismissed.connect(lambda: emits.append(1))
        toast._no_button.click()
        toast.close()
        assert emits == []
