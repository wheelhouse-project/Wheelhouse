"""Input-side tests for the mouse wheel handler
(wh-voice-access-parity.2.3, part one -- discrete scrolling only).

``UIActionHandler.scroll_wheel`` is the Input-process end of the spoken scroll
commands. It is deliberately shaped like ``hotkey_action`` rather than like the
mouse-grid pointer handlers:

* It is NOT in ``_HANDLES_OWN_RESPONSE``, so it emits no ``MouseActionResponse``.
  The generic dispatcher in input_proc.py answers a waiting Logic request with
  its heuristic acknowledgement, exactly as it does for a hotkey.
* It is NOT gated by the voice-clicking config. That gate exists because the
  grid's pointer actions move the physical pointer and click with it. A wheel
  notch presses nothing and moves nothing, and a user who turned voice clicking
  off still has to be able to read a long page.
* It never raises. A handler that raises inside the Input process loop turns a
  bad message into a logged dispatch error instead of a scroll that did nothing.

The SendInput boundary is injected as ``_mouse_scroll_seam`` so these tests
never synthesise real input.
"""

from __future__ import annotations

import logging
import threading
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

        yield UIActionHandler(
            response_queue=MagicMock(), config={"ui_actions": {}},
        )


def test_scroll_wheel_calls_the_seam_with_direction_and_count(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_scroll_seam = seam

    handler.scroll_wheel(direction="down", clicks=3)

    seam.assert_called_once_with("down", 3)


def test_scroll_wheel_defaults_to_one_notch(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_scroll_seam = seam

    handler.scroll_wheel(direction="up")

    seam.assert_called_once_with("up", 1)


def test_scroll_wheel_emits_no_response(handler):
    """It is not in _HANDLES_OWN_RESPONSE; the generic dispatcher answers."""
    handler._mouse_scroll_seam = MagicMock(return_value=(True, None))

    handler.scroll_wheel(direction="down", clicks=1)

    handler.response_queue.put.assert_not_called()


def test_scroll_wheel_runs_with_voice_clicking_disabled(handler):
    """Scrolling presses nothing, so the click gate must not block it."""
    from ui.click_config import ClickConfig

    handler._click_config = ClickConfig.from_raw({"enabled": False})
    seam = MagicMock(return_value=(True, None))
    handler._mouse_scroll_seam = seam

    handler.scroll_wheel(direction="down", clicks=2)

    seam.assert_called_once_with("down", 2)


def test_scroll_wheel_invalidates_the_shadow_buffer(handler):
    """A wheel notch over a combo box or a spinner changes its value.

    The remembered copy of the focused control's text cannot survive that, and
    the keyboard listener never sees a wheel event, so the invalidation has to
    happen here (the same reasoning hotkey_action records).
    """
    handler._mouse_scroll_seam = MagicMock(return_value=(True, None))

    handler.scroll_wheel(direction="down")

    handler.buffer_manager.invalidate.assert_called_once()


def test_scroll_wheel_survives_a_seam_refusal(handler):
    handler._mouse_scroll_seam = MagicMock(
        return_value=(False, "invalid_direction")
    )

    handler.scroll_wheel(direction="sideways")

    handler.response_queue.put.assert_not_called()


def test_scroll_wheel_survives_a_raising_seam(handler):
    """A raise inside the Input loop must not escape the handler."""
    handler._mouse_scroll_seam = MagicMock(side_effect=OSError("boom"))

    handler.scroll_wheel(direction="down", clicks=1)

    handler.response_queue.put.assert_not_called()


def test_the_default_seam_is_the_sendinput_primitive():
    """A fresh handler's seam reaches the real wheel primitive."""
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):

        from ui.ui_action_handler import UIActionHandler, _win32_scroll_wheel

        fresh = UIActionHandler(
            response_queue=MagicMock(), config={"ui_actions": {}},
        )
        assert fresh._mouse_scroll_seam is _win32_scroll_wheel


def test_the_win32_seam_delegates_to_the_primitive():
    from ui.ui_action_handler import _win32_scroll_wheel

    with patch("utils.win_input_sender.scroll_wheel") as primitive:
        primitive.return_value = (True, None)

        assert _win32_scroll_wheel("up", 2) == (True, None)

    primitive.assert_called_once_with("up", 2)


def test_the_win32_seam_reports_a_raising_primitive():
    """The seam is fail-soft: a raise becomes a refusal, not an exception.

    ``sendinput_error`` is the reason the sibling ``_win32_*`` seams already
    return on this path; the wheel seam uses the same word so one reason name
    covers the whole class.
    """
    from ui.ui_action_handler import _win32_scroll_wheel

    with patch("utils.win_input_sender.scroll_wheel") as primitive:
        primitive.side_effect = OSError("boom")

        succeeded, reason = _win32_scroll_wheel("down", 1)

    assert succeeded is False
    assert reason == "sendinput_error"


class TestTheDiscreteScrollWritesItsOwnNotice:
    """wh-wheel-refusal-notice, step 1.

    Until this change the discrete spoken scroll had NO written notice. Its
    only user-visible signal was the generic ``[ERROR] utils.win_input_sender``
    box that ``ErrorNotificationHandler`` shows for every ERROR record. Step 2
    demotes that record to WARNING, which would have left a refused discrete
    scroll completely silent, so the handler has to write its own notice
    first.

    EVERY refusal writes the notice, including the two validation
    refusals (``invalid_direction`` / ``invalid_clicks``). Those two
    used to keep their ERROR in the primitive and skip the written
    notice, on the reasoning that the generic box was already the
    user's one report. wh-wheel-refusal-notice.1.7 measured that
    reasoning and it is false: ErrorNotificationHandler.emit keys on
    (logger name, level, message) and returns without submitting
    anything when the same key repeats inside rate_limit_seconds,
    which utils/logging_setup.py sets to 10. A malformed wheel action
    repeated inside ten seconds produced the same key, no box, and no
    written notice either -- ZERO notices. An ERROR record is not
    proof the user was told, so nothing here relies on one any more.
    """

    def test_a_short_send_shows_one_written_notice(self, handler):
        from ui.ui_action_handler import SCROLL_REFUSED_MESSAGE

        handler._mouse_scroll_seam = MagicMock(
            return_value=(False, "sendinput_short")
        )

        with patch(f"{_MOD}.send_notice") as notice:
            handler.scroll_wheel(direction="down", clicks=3)

        assert notice.call_count == 1
        assert notice.call_args.args[1] == SCROLL_REFUSED_MESSAGE

    def test_a_sendinput_error_shows_one_written_notice(self, handler):
        from ui.ui_action_handler import SCROLL_REFUSED_MESSAGE

        handler._mouse_scroll_seam = MagicMock(
            return_value=(False, "sendinput_error")
        )

        with patch(f"{_MOD}.send_notice") as notice:
            handler.scroll_wheel(direction="up", clicks=1)

        assert notice.call_count == 1
        assert notice.call_args.args[1] == SCROLL_REFUSED_MESSAGE

    def test_an_unknown_refusal_reason_still_shows_the_notice(self, handler):
        """Notice by default. A reason name added later must not be silent."""
        handler._mouse_scroll_seam = MagicMock(
            return_value=(False, "some_future_reason")
        )

        with patch(f"{_MOD}.send_notice") as notice:
            handler.scroll_wheel(direction="down", clicks=1)

        assert notice.call_count == 1

    def test_a_raising_seam_shows_the_notice_and_logs_a_warning(
        self, handler, caplog
    ):
        """The defensive branch keeps the count at one, so it cannot log ERROR.

        ``_win32_scroll_wheel`` already turns a raising primitive into
        ``(False, "sendinput_error")``, so this branch only runs if the seam
        itself raises. An ERROR here would show its own box beside the notice.
        """
        handler._mouse_scroll_seam = MagicMock(side_effect=OSError("boom"))

        with patch(f"{_MOD}.send_notice") as notice:
            with caplog.at_level(logging.DEBUG, logger=_MOD):
                handler.scroll_wheel(direction="down", clicks=1)

        assert notice.call_count == 1
        levels = {
            r.levelname for r in caplog.records
            if r.name == _MOD and "unexpected error" in r.getMessage()
        }
        assert levels == {"WARNING"}

    def test_a_successful_scroll_shows_no_notice(self, handler):
        handler._mouse_scroll_seam = MagicMock(return_value=(True, None))

        with patch(f"{_MOD}.send_notice") as notice:
            handler.scroll_wheel(direction="down", clicks=1)

        notice.assert_not_called()

    def test_an_invalid_direction_shows_one_written_notice(self, handler):
        """wh-wheel-refusal-notice.1.7: the rate limiter can drop the
        generic box, so this path writes its own notice like every
        other refusal.
        """
        from ui.ui_action_handler import SCROLL_REFUSED_MESSAGE

        handler._mouse_scroll_seam = MagicMock(
            return_value=(False, "invalid_direction")
        )

        with patch(f"{_MOD}.send_notice") as notice:
            handler.scroll_wheel(direction="sideways")

        assert notice.call_count == 1
        assert notice.call_args.args[1] == SCROLL_REFUSED_MESSAGE

    def test_an_invalid_notch_count_shows_one_written_notice(self, handler):
        from ui.ui_action_handler import SCROLL_REFUSED_MESSAGE

        handler._mouse_scroll_seam = MagicMock(
            return_value=(False, "invalid_clicks")
        )

        with patch(f"{_MOD}.send_notice") as notice:
            handler.scroll_wheel(direction="down", clicks=0)

        assert notice.call_count == 1
        assert notice.call_args.args[1] == SCROLL_REFUSED_MESSAGE

    def test_the_notice_carries_the_shared_title(self, handler):
        """One title for every Wheelhouse notice; a reader hears it first."""
        from ui.continuous_scroll import NOTICE_TITLE

        handler._mouse_scroll_seam = MagicMock(
            return_value=(False, "sendinput_short")
        )

        with patch(f"{_MOD}.send_notice") as notice:
            handler.scroll_wheel(direction="down", clicks=1)

        assert notice.call_args.args[0] == NOTICE_TITLE

    def test_the_notice_says_the_exact_words_the_user_hears(self, handler):
        """The wording itself, not just the constant.

        Comparing the notice against SCROLL_REFUSED_MESSAGE proves only that
        the handler used the constant: change the constant and both sides of
        that assertion move together. The mutation gate found exactly that --
        the "message-changed" mutation survived until this test existed. A
        screen reader reads this sentence out, so the sentence is pinned.
        """
        handler._mouse_scroll_seam = MagicMock(
            return_value=(False, "sendinput_short")
        )

        with patch(f"{_MOD}.send_notice") as notice:
            handler.scroll_wheel(direction="down", clicks=1)

        assert notice.call_args.args[1] == "Scrolling failed"

    def test_a_raising_notice_never_escapes_the_handler(self, handler):
        """The handler still may not raise inside the Input command loop."""
        handler._mouse_scroll_seam = MagicMock(
            return_value=(False, "sendinput_short")
        )

        with patch(f"{_MOD}.send_notice", side_effect=OSError("boom")):
            handler.scroll_wheel(direction="down", clicks=1)

        handler.response_queue.put.assert_not_called()


class TestTheNoticeNamesTheScrollThatSentIt:
    """wh-wheel-refusal-notice: ``send_notice`` is shared by two callers now.

    Its log lines used to be written as "continuous scroll: ...", which was
    accurate while the continuous scroller was the only caller. The discrete
    spoken scroll calls it too now, so a caller that reported nothing has to
    say which scroll it was.
    """

    def test_send_notice_names_the_caller_in_its_log_line(self, caplog):
        from ui.continuous_scroll import send_notice

        with patch(
            "utils.logging_setup.get_notifier_worker", return_value=None
        ):
            with caplog.at_level(logging.INFO, logger="ui.continuous_scroll"):
                send_notice("Wheelhouse", "hello", source="discrete scroll")

        lines = [r.getMessage() for r in caplog.records]
        assert any(line.startswith("discrete scroll:") for line in lines)
        assert not any(line.startswith("continuous scroll:") for line in lines)

    def test_send_notice_defaults_to_the_continuous_scroll(self, caplog):
        """The existing caller keeps its wording without passing anything."""
        from ui.continuous_scroll import send_notice

        with patch(
            "utils.logging_setup.get_notifier_worker", return_value=None
        ):
            with caplog.at_level(logging.INFO, logger="ui.continuous_scroll"):
                send_notice("Wheelhouse", "hello")

        lines = [r.getMessage() for r in caplog.records]
        assert any(line.startswith("continuous scroll:") for line in lines)

    def test_the_handler_names_the_discrete_scroll_when_it_reports(
        self, handler
    ):
        """The handler's own call has to pass the source, or the log lies."""
        handler._mouse_scroll_seam = MagicMock(
            return_value=(False, "sendinput_short")
        )

        with patch(f"{_MOD}.send_notice") as notice:
            handler.scroll_wheel(direction="down", clicks=1)

        assert notice.call_args.kwargs.get("source") == "discrete scroll"


class TestTheExclusionListComesFromThePrimitive:
    """wh-wheel-refusal-notice.1.1 -- the reviewer's finding.

    The primitive owns the published list, and this module must read
    that object rather than copy it. Copying would let the two drift: a
    refusal reason added later could be listed in
    ``utils/win_input_sender.py`` and never reach a hand-written copy
    here.

    WHAT THE LIST NOW MEANS (wh-wheel-refusal-notice.1.23). This
    docstring used to say that an ERROR record already shows the user a
    generic box, and that the list therefore decided which refusals get
    a written notice. Round 5 reversed exactly that: ``.1.7`` showed an
    ERROR record is NOT proof the user was told, because
    ``utils/error_notifier.py`` returns on its ten-second rate limit
    before it builds anything. The published list has been EMPTY ever
    since -- no wheel refusal logs at ERROR, and no refusal may be
    excluded from its caller's own written notice. The test below is
    now an ownership pin on a deliberately empty object, and nothing
    here selects refusals by level.

    The two-box history is still why the list exists: an ERROR record
    from the primitive plus a written notice from the caller gave the
    user two boxes for one refusal. Keeping the list empty is what
    prevents that, not choosing which refusals are loud.

    ``is`` rather than ``==`` on purpose. Equality passes for a copy that
    happens to match today; identity passes only while this module reads the
    primitive's own list.
    """

    def test_the_handler_reads_the_primitive_s_own_list(self):
        from ui import ui_action_handler
        from utils import win_input_sender as wis

        assert (
            ui_action_handler._WHEEL_VALIDATION_REFUSALS
            is wis.WHEEL_REFUSALS_THAT_LOG_ERROR
        )


class TestARaisingPrimitiveThroughTheDefaultSeam:
    """wh-wheel-refusal-notice.1.3: the wrapper is a SECOND refusal producer.

    Every other test in this file replaces ``_mouse_scroll_seam``, so the real
    ``_win32_scroll_wheel`` never runs and the level of its own record went
    unchecked through two review rounds. It turns a raising primitive into
    ``(False, "sendinput_error")`` -- a reason OUTSIDE
    ``WHEEL_REFUSALS_THAT_LOG_ERROR``, so both callers write their own notice
    for it -- and it logged that conversion at ERROR, which adds the generic
    ``ErrorNotificationHandler`` box beside the written notice. Two boxes,
    which is the whole of what this bead exists to remove.

    Both tests leave the default seam in place and make the PRIMITIVE raise,
    so the wrapper is the code under test on the discrete route and on the
    continuous one.
    """

    @staticmethod
    def _seam_records(caplog):
        return [
            r for r in caplog.records
            if r.name == _MOD and "seam failed" in r.getMessage()
        ]

    @staticmethod
    def _error_records(caplog):
        return [
            f"{r.name}: {r.getMessage()}"
            for r in caplog.records if r.levelname == "ERROR"
        ]

    def test_the_discrete_scroll_shows_one_notice_and_logs_no_error(
        self, handler, caplog
    ):
        from ui.ui_action_handler import (
            SCROLL_REFUSED_MESSAGE, _win32_scroll_wheel,
        )

        assert handler._mouse_scroll_seam is _win32_scroll_wheel, (
            "this test is about the DEFAULT seam; something replaced it"
        )

        with patch(
            "utils.win_input_sender.scroll_wheel", side_effect=OSError("boom")
        ):
            with patch(f"{_MOD}.send_notice") as notice:
                with caplog.at_level(logging.DEBUG):
                    handler.scroll_wheel(direction="down", clicks=1)

        assert notice.call_count == 1
        assert notice.call_args.args[1] == SCROLL_REFUSED_MESSAGE
        records = self._seam_records(caplog)
        assert records, "the wrapper swallowed the raise without a word"
        assert {r.levelname for r in records} == {"WARNING"}
        assert all(r.exc_info for r in records), (
            "the demotion must keep the exception context; without it the "
            "log says a wheel call failed and never says why"
        )
        assert self._error_records(caplog) == [], (
            "an ERROR record shows the generic ErrorNotificationHandler box "
            "beside the written notice, which is two boxes: "
            f"{self._error_records(caplog)}"
        )

    def test_the_continuous_scroll_shows_one_notice_and_logs_no_error(
        self, caplog
    ):
        from ui.continuous_scroll import (
            ContinuousScroller, NOTICE_TITLE, WHEEL_FAILED_MESSAGE,
        )
        from ui.ui_action_handler import _win32_scroll_wheel

        # wh-wheel-refusal-notice.1.5: wait for the NOTICE, not for
        # is_running(). _stop_and_say calls _clear_if_current(run)
        # first, which sets _run to None, and is_running() reads that
        # -- so it goes False BEFORE the notifier is called. A wait
        # on is_running() can end inside that window, and stop() then
        # owns no run to join, so the assertions below could read an
        # empty list while the notice was about to be appended.
        # wh-wheel-refusal-notice.1.11: keep the thread the notifier
        # ran on. The Event above says the FIRST notice arrived, not that
        # the timer thread has finished, and stop() cannot close that gap
        # -- _clear_if_current has already set _run to None by the time
        # _stop_and_say calls the notifier, so stop() owns no run to
        # join. A second notice sent after the Event is set would land
        # after the assertions had already read the list.
        notices = []
        said = threading.Event()
        saying = []

        def notifier(title, message):
            saying.append(threading.current_thread())
            notices.append((title, message))
            said.set()

        scroller = ContinuousScroller(
            _win32_scroll_wheel, notifier, tick_seconds=0.002,
        )

        with patch(
            "utils.win_input_sender.scroll_wheel", side_effect=OSError("boom")
        ):
            with caplog.at_level(logging.DEBUG):
                try:
                    scroller.start("down")
                    assert said.wait(timeout=10.0), (
                        "the continuous scroll never said the wheel "
                        "would not turn"
                    )
                finally:
                    scroller.stop()

        # Join what actually spoke, before reading what it said.
        assert saying, "the notifier never ran, so there is nothing to join"
        for thread in saying:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=10.0)
            assert not thread.is_alive(), (
                "the scroller's timer thread was still running after "
                f"stop() returned, so {notices} is not the final list"
            )

        assert notices == [(NOTICE_TITLE, WHEEL_FAILED_MESSAGE)], (
            "the continuous scroll must stop and say so exactly once; it "
            f"said {notices}"
        )
        records = self._seam_records(caplog)
        assert records, "the wrapper swallowed the raise without a word"
        assert {r.levelname for r in records} == {"WARNING"}
        assert self._error_records(caplog) == [], (
            "an ERROR record shows the generic ErrorNotificationHandler box "
            "beside the scroller's own notice, which is two boxes: "
            f"{self._error_records(caplog)}"
        )
