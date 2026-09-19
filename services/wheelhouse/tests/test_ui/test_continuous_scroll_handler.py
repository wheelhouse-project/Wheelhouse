"""Input-side handlers for continuous scrolling
(wh-voice-access-parity.2.3.3, part two).

``UIActionHandler.start_continuous_scroll`` and
``UIActionHandler.stop_continuous_scroll`` are the Input-process end of the
spoken continuous-scroll commands. They own the one
``ui.continuous_scroll.ContinuousScroller`` this process may have running.

Both are shaped like ``scroll_wheel``:

* Neither is in ``_HANDLES_OWN_RESPONSE``, so neither emits its own response.
  The generic dispatcher in input_proc.py answers a waiting Logic request.
* Neither is gated by the voice-clicking config, for the same reason the
  discrete wheel handler is not: a wheel notch presses nothing and moves
  nothing.
* Neither raises. A handler that raised inside the Input command loop would
  turn a bad message into a logged dispatch error.

The one thing these handlers must do that ``scroll_wheel`` does not is return
IMMEDIATELY. The start handler starts a thread and returns, which is what
keeps the single Input command loop free for the stop word.

Acceptance item 5 lives here as well: exactly one continuous scroll at a
time, and a DISCRETE scroll command stops the continuous one -- a user who
says "scroll down 3" during a continuous scroll wants three notches, not
three notches added to a scroll that is still running.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

_MOD = "ui.ui_action_handler"


@pytest.fixture
def handler():
    """Build a UIActionHandler with its specialist components mocked."""
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):

        from ui.ui_action_handler import UIActionHandler

        made = UIActionHandler(
            response_queue=MagicMock(), config={"ui_actions": {}},
        )
        try:
            yield made
        finally:
            made.stop_continuous_scroll()


@pytest.fixture
def scroller(handler):
    """Replace the handler's real scroller with a recording stand-in."""
    fake = MagicMock()
    fake.start.return_value = True
    fake.stop.return_value = True
    handler._continuous_scroller = fake
    return fake


class TestTheHandlerOwnsOneScroller:
    def test_the_handler_builds_a_scroller_at_construction(self, handler):
        from ui.continuous_scroll import ContinuousScroller

        assert isinstance(handler._continuous_scroller, ContinuousScroller)

    def test_the_scroller_turns_the_wheel_through_the_same_seam(self, handler):
        """One SendInput boundary for both scroll commands, so a test that
        injects a fake seam is not silently bypassed by the continuous one."""
        seam = MagicMock(return_value=(True, None))
        handler._mouse_scroll_seam = seam
        # The scroller reads the seam through the handler, so replacing the
        # handler's attribute must reach it.
        handler._continuous_scroller._seam("down", 2)
        seam.assert_called_once_with("down", 2)


class TestStartHandler:
    @pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
    def test_it_starts_the_scroller_in_the_given_direction(
        self, handler, scroller, direction
    ):
        handler.start_continuous_scroll(direction=direction)
        scroller.start.assert_called_once_with(direction)

    def test_it_emits_no_response(self, handler, scroller):
        handler.start_continuous_scroll(direction="down")
        handler.response_queue.put.assert_not_called()

    def test_it_never_raises_when_the_scroller_fails(self, handler, scroller):
        scroller.start.side_effect = RuntimeError("thread refused")
        handler.start_continuous_scroll(direction="down")

    def test_it_never_raises_on_a_malformed_message(self, handler, scroller):
        handler.start_continuous_scroll()
        handler.start_continuous_scroll(direction=None)
        handler.start_continuous_scroll(direction="sideways")

    def test_it_ignores_unexpected_extra_fields(self, handler, scroller):
        """A message with a field this handler does not know must not raise."""
        handler.start_continuous_scroll(direction="down", speed="fast")
        scroller.start.assert_called_once_with("down")

    def test_it_invalidates_the_remembered_text(self, handler, scroller):
        """A wheel notch over a spinner or a slider changes that control's
        value, so the remembered copy of the focused control cannot survive
        it -- the same reason the discrete wheel handler invalidates."""
        handler.start_continuous_scroll(direction="down")
        handler.buffer_manager.invalidate.assert_called()


class TestStopHandler:
    def test_it_stops_the_scroller(self, handler, scroller):
        handler.stop_continuous_scroll()
        scroller.stop.assert_called_once_with()

    def test_it_emits_no_response(self, handler, scroller):
        handler.stop_continuous_scroll()
        handler.response_queue.put.assert_not_called()

    def test_it_never_raises_when_the_scroller_fails(self, handler, scroller):
        scroller.stop.side_effect = RuntimeError("join failed")
        handler.stop_continuous_scroll()

    def test_stopping_with_nothing_running_is_harmless(self, handler, scroller):
        scroller.stop.return_value = False
        handler.stop_continuous_scroll()
        handler.stop_continuous_scroll()
        assert scroller.stop.call_count == 2

    def test_it_ignores_unexpected_extra_fields(self, handler, scroller):
        handler.stop_continuous_scroll(direction="down")
        scroller.stop.assert_called_once_with()


class TestADiscreteScrollStopsTheContinuousOne:
    """Acceptance item 5, the half that crosses the two handlers."""

    def test_a_discrete_scroll_stops_a_running_continuous_scroll(
        self, handler, scroller
    ):
        handler._mouse_scroll_seam = MagicMock(return_value=(True, None))
        handler.scroll_wheel(direction="down", clicks=3)
        scroller.stop.assert_called_once_with()

    def test_the_discrete_notches_are_still_sent(self, handler, scroller):
        seam = MagicMock(return_value=(True, None))
        handler._mouse_scroll_seam = seam
        handler.scroll_wheel(direction="up", clicks=2)
        seam.assert_called_once_with("up", 2)

    def test_a_failing_stop_does_not_lose_the_discrete_scroll(
        self, handler, scroller
    ):
        """The notches the user asked for matter more than the stop."""
        scroller.stop.side_effect = RuntimeError("join failed")
        seam = MagicMock(return_value=(True, None))
        handler._mouse_scroll_seam = seam
        handler.scroll_wheel(direction="down", clicks=1)
        seam.assert_called_once_with("down", 1)


class TestTheHandlerPassesItsShutdownSignal:
    """The handler hands its shutdown signal to the scroller it owns
    (wh-voice-access-parity.2.3.3.2.10).

    The Input loop gives the handler the process's shared shutdown signal, and
    the handler is the only thing that can give it to the scroller. Without
    that second handoff the timer thread never sees a shutdown, and the wheel
    keeps turning through the launcher's grace period even though the loop was
    told correctly. Tested through real scrolling rather than by reading the
    attribute, so a handoff that stores the signal somewhere the timer never
    reads still fails here.
    """

    @staticmethod
    def _build(shutdown):
        with patch(f"{_MOD}.TextPerfector"), \
             patch(f"{_MOD}.ClipboardOperations"), \
             patch(f"{_MOD}.WindowFocusManager"), \
             patch(f"{_MOD}.SelectionTransformer"), \
             patch(f"{_MOD}.UtteranceClipboardManager"), \
             patch(f"{_MOD}.ShadowBufferManager"), \
             patch(f"{_MOD}.TerminalEditorProxy"), \
             patch(f"{_MOD}.InsertionRouter"):

            from ui.ui_action_handler import UIActionHandler

            return UIActionHandler(
                response_queue=MagicMock(),
                config={"ui_actions": {}},
                shutdown_event=shutdown,
            )

    def test_a_shutdown_stops_a_scroll_the_handler_started(self):
        import threading
        import time

        shutdown = threading.Event()
        made = self._build(shutdown)
        turns = []
        lock = threading.Lock()

        def wheel(direction, clicks):
            with lock:
                turns.append((direction, clicks))
            return (True, None)

        def count():
            with lock:
                return len(turns)

        made._mouse_scroll_seam = wheel
        try:
            made.start_continuous_scroll(direction="down")
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and count() < 1:
                time.sleep(0.005)
            assert count() >= 1, (
                "the handler never started a scroll, so this test proves "
                "nothing about stopping one"
            )

            shutdown.set()
            deadline = time.monotonic() + 10.0
            while (
                time.monotonic() < deadline
                and made._continuous_scroller.is_running()
            ):
                time.sleep(0.005)
            assert made._continuous_scroller.is_running() is False, (
                "the handler's scroller kept running after shutdown was "
                "signalled, so the handler did not pass the signal on"
            )
            settled = count()
            time.sleep(0.3)
            assert count() == settled, (
                "the wheel turned again after shutdown was signalled: "
                f"{settled} calls at the stop, {count()} after"
            )
        finally:
            made.stop_continuous_scroll()
