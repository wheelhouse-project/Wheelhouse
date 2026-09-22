"""A dictated word must survive a target window that closes before the paste.

Bead wh-paste-target-window-vanished. The incident: the user dictated a
word into a Brave menu item, the menu closed between the capture and the
paste, GetAncestor raised error 1400 on the captured handle, the pre-send
proof refused, no Ctrl+V was ever sent, and the word was delivered
nowhere.

The fix captures the target once more and sends the word through the
normal route, but only when three things hold together:

  - the strategy that failed is ClipboardOnlyStrategy. Its failure
    branch is reached only when no keystroke fired, so one more attempt
    cannot deliver the same word twice.
  - the strategy reported that the window it tried to paste into no
    longer exists.
  - the newly captured target belongs to the same application as the
    captured one (boss ruling, option 2). The word must not land in a
    different program from the one the user spoke it into.

Every test here drives the real UIActionHandler._execute_insert_with_ack
with its dependencies mocked, the same shape
tests/test_ui/test_phase1_soft_fallback.py uses.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ui.context import UIContext
from ui.strategies.base import InsertionResult

_MOD = "ui.ui_action_handler"

# The handler config needs only the keys the constructor reads.
_CONFIG = {
    "ui_actions": {
        "timing": {"utterance_clipboard_timeout_seconds": 1.0},
        "verified_unicode": {"max_chars": 50},
        "foreground_check": {"same_process_browser_names_extend": []},
        "text_target": {},
    }
}


def _context(process_name: str, hwnd: int) -> UIContext:
    """A capture whose focused control reports one window handle."""
    control = MagicMock()
    top_level = MagicMock()
    top_level.NativeWindowHandle = hwnd
    control.GetTopLevelControl.return_value = top_level
    return UIContext(
        focused_control=control,
        is_flutter=False,
        is_terminal=False,
        process_name=process_name,
        class_name="Chrome_WidgetWin_1",
        process_id=4242,
    )


def _clipboard_only_failure(*, window_gone: bool) -> InsertionResult:
    """The result ClipboardOnlyStrategy returns when no keystroke fired.

    clipboard_dirty stays True because the clipboard write happens
    before the pre-send proof refuses, so the utterance manager still
    has to restore the user's clipboard.

    wh-lost-word-neighbour-paths added ``delivered_nothing`` to
    InsertionResult, and the strategy sets it True on this same
    no-keystroke branch, next to the window probe. It is set here for
    the same reason every other field is: this helper stands for one
    real branch of one real strategy, and a stub that reports less than
    the branch does would make these tests measure a result production
    never produces.
    """
    return InsertionResult(
        success=False,
        clipboard_dirty=True,
        retry_outcome="unverified",
        target_window_gone=window_gone,
        delivered_nothing=True,
    )


_DELIVERED = InsertionResult(
    success=True, clipboard_dirty=True, retry_outcome="verified",
)


@pytest.fixture
def handler():
    """A UIActionHandler whose router always returns ClipboardOnly."""
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):

        from ui.ui_action_handler import UIActionHandler

        built = UIActionHandler(response_queue=MagicMock(), config=_CONFIG)

    built.response = MagicMock()
    built.router.get_strategy = MagicMock(
        return_value=built.clipboard_only_strategy,
    )
    return built


class TestTheWordSurvivesADeadTargetWindow:
    """A1: the word is captured again once and takes the normal route."""

    def test_a_dead_target_window_costs_one_more_capture_not_the_word(
        self, handler,
    ):
        handler.clipboard_only_strategy.insert = MagicMock(side_effect=[
            _clipboard_only_failure(window_gone=True),
            _DELIVERED,
        ])
        captures = [
            _context("brave.exe", 0x1111),   # the menu item, now destroyed
            _context("brave.exe", 0x2222),   # the window behind it
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is True
        assert capture.call_count == 2
        assert handler.clipboard_only_strategy.insert.call_count == 2
        handler.response.send_error.assert_not_called()
        handler.response.send_success.assert_called_once()

    def test_the_second_attempt_routes_the_newly_captured_target(
        self, handler,
    ):
        """The retry asks the router again, so the new target is judged
        on its own. The captured target was a menu item the approved-
        control list refuses; the window behind it is an ordinary text
        target, and only a fresh routing decision can tell them apart.
        """
        handler.clipboard_only_strategy.insert = MagicMock(side_effect=[
            _clipboard_only_failure(window_gone=True),
            _DELIVERED,
        ])
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            handler._execute_insert_with_ack("hello", request_id="r1")

        assert handler.router.get_strategy.call_count == 2
        second_context = handler.router.get_strategy.call_args_list[1][0][0]
        assert second_context is captures[1]
        assert handler.clipboard_only_strategy.insert.call_args_list[1][0][1] \
            is captures[1]


class TestTheRefusalForALiveWindowIsUnchanged:
    """A2: bead wh-oe7u.3's refusal keeps its behaviour."""

    def test_a_live_window_that_lost_the_foreground_is_still_refused(
        self, handler,
    ):
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_failure(window_gone=False),
        )
        # One spare capture, from the same program, so an attempt that
        # must NOT happen runs for real instead of raising StopIteration
        # on an exhausted list. The except arm of
        # _execute_insert_with_ack turns that exception into one
        # send_error and a False return, which is exactly what this test
        # asserts, so a short list lets a broken retry pass as a refusal.
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is False
        assert capture.call_count == 1
        assert handler.clipboard_only_strategy.insert.call_count == 1
        handler.response.send_error.assert_called_once()

    def test_another_strategy_that_fails_is_never_retried(self, handler):
        """Only ClipboardOnlyStrategy guarantees no keystroke fired.

        A strategy that sends first and verifies afterwards could have
        delivered the word already, so a retry there could type it
        twice. The retry is gated on the strategy for that reason.
        """
        other = MagicMock()
        other.insert = MagicMock(
            return_value=_clipboard_only_failure(window_gone=True),
        )
        handler.router.get_strategy = MagicMock(return_value=other)
        # One spare capture, from the same program, so an attempt that
        # must NOT happen runs for real instead of raising StopIteration
        # on an exhausted list. The except arm of
        # _execute_insert_with_ack turns that exception into one
        # send_error and a False return, which is exactly what this test
        # asserts, so a short list lets a broken retry pass as a refusal.
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is False
        assert capture.call_count == 1
        assert other.insert.call_count == 1
        handler.response.send_error.assert_called_once()


class TestTheRetryHappensAtMostOnce:
    """A3: one retry per word, and a failed retry ends as today."""

    def test_a_failed_retry_ends_as_today_and_never_loops(self, handler):
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_failure(window_gone=True),
        )
        # One spare capture, from the same program, so an attempt that
        # must NOT happen runs for real instead of raising StopIteration
        # on an exhausted list. The except arm of
        # _execute_insert_with_ack turns that exception into one
        # send_error and a False return, which is exactly what this test
        # asserts, so a short list lets a broken retry pass as a refusal.
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
            _context("brave.exe", 0x3333),
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is False
        assert capture.call_count == 2
        assert handler.clipboard_only_strategy.insert.call_count == 2
        handler.response.send_error.assert_called_once()
        handler.response.send_success.assert_not_called()


class TestTheWordStaysInsideItsOwnApplication:
    """Boss ruling, option 2: same application, or the word fails."""

    def test_a_new_target_in_another_application_does_not_get_the_word(
        self, handler,
    ):
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_failure(window_gone=True),
        )
        captures = [
            _context("brave.exe", 0x1111),
            _context("notepad.exe", 0x3333),
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is False
        assert handler.clipboard_only_strategy.insert.call_count == 1
        handler.response.send_error.assert_called_once()

    def test_the_refused_retry_leaves_the_remembered_window_alone(
        self, handler,
    ):
        """The process check runs before remember_target.

        A remembered window the insertion never wrote to is the hazard
        the RejectedInsertionStrategy branch of this same method
        describes: a later retraction compares the remembered window
        against the foreground, passes, and deletes text this program
        did not write.
        """
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_failure(window_gone=True),
        )
        captures = [
            _context("brave.exe", 0x1111),
            _context("notepad.exe", 0x3333),
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            handler._execute_insert_with_ack("hello", request_id="r1")

        assert handler.window_manager.remember_target.call_count == 1

    def test_a_capture_that_names_no_application_refuses_the_retry(
        self, handler,
    ):
        """An empty process name means the read failed, so refuse.

        capture_context leaves process_name empty when the focused
        control raises or psutil cannot name the process. Two empty
        names must not compare equal and let the word through.
        """
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_failure(window_gone=True),
        )
        captures = [_context("", 0x1111), _context("", 0x3333)]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is False
        assert handler.clipboard_only_strategy.insert.call_count == 1
        handler.response.send_error.assert_called_once()


# --- The strategy side: who sets target_window_gone, and when --------------


class _RefusingClipboard:
    """A ClipboardOperations stand-in whose paste refuses.

    ClipboardOnlyStrategy snapshots and restores four accumulated_*
    fields around verified_paste, so the stand-in has to carry them.
    keystroke_fires decides the only thing these tests care about:
    whether last_paste_was_sent is True when verified_paste returns.
    """

    def __init__(self, *, keystroke_fires: bool, paste_returns: bool = False):
        self._keystroke_fires = keystroke_fires
        self._paste_returns = paste_returns
        self.accumulated_paste_chars = 0
        self.accumulated_paste_clusters = 0
        self.accumulated_paste_was_qt = False
        self.accumulated_has_grapheme_unsafe = False
        self.last_paste_was_sent = False
        self.last_paste_was_optimistic = False

    def verified_paste(self, text, *_args, **_kwargs):
        self.last_paste_was_sent = self._keystroke_fires
        self.last_paste_was_optimistic = False
        return self._paste_returns


def _identity_context(root: int) -> UIContext:
    """A capture whose target identity names one top-level window."""
    from ui.target_identity import TargetIdentity

    control = MagicMock()
    return UIContext(
        focused_control=control,
        is_flutter=False,
        is_terminal=False,
        process_name="brave.exe",
        class_name="Chrome_WidgetWin_1",
        target_identity=TargetIdentity(hwnd=root, root=root),
    )


class TestOnlyTheNoKeystrokeBranchReportsTheWindowGone:
    """The field means "no keystroke fired AND the window is gone".

    A branch that fired the keystroke must never set it, because the
    handler retries on it and the word would be delivered twice.
    """

    def _strategy(self, clipboard):
        from ui.strategies.specific import ClipboardOnlyStrategy

        return ClipboardOnlyStrategy(clipboard, MagicMock(), text_perfector=None)

    def test_the_no_keystroke_refusal_asks_about_the_captured_window(self):
        from ui.strategies import specific

        clipboard = _RefusingClipboard(keystroke_fires=False)
        probe = MagicMock(return_value=True)

        with patch.object(specific, "hwnd_no_longer_exists", probe):
            result = self._strategy(clipboard).insert(
                "hello", _identity_context(0x5555),
            )

        assert result.success is False
        assert result.target_window_gone is True
        probe.assert_called_once_with(0x5555)

    def test_a_live_window_refusal_reports_no_window_gone(self):
        from ui.strategies import specific

        clipboard = _RefusingClipboard(keystroke_fires=False)
        probe = MagicMock(return_value=False)

        with patch.object(specific, "hwnd_no_longer_exists", probe):
            result = self._strategy(clipboard).insert(
                "hello", _identity_context(0x5555),
            )

        assert result.success is False
        assert result.target_window_gone is False
        probe.assert_called_once_with(0x5555)

    def test_a_failure_after_the_keystroke_never_reports_the_window_gone(self):
        """This is the branch a retry would turn into a double paste."""
        from ui.strategies import specific

        clipboard = _RefusingClipboard(keystroke_fires=True)
        probe = MagicMock(return_value=True)

        with patch.object(specific, "hwnd_no_longer_exists", probe):
            result = self._strategy(clipboard).insert(
                "hello", _identity_context(0x5555),
            )

        assert result.success is True
        assert result.target_window_gone is False
        probe.assert_not_called()

    def test_a_delivered_paste_never_reports_the_window_gone(self):
        from ui.strategies import specific

        clipboard = _RefusingClipboard(
            keystroke_fires=True, paste_returns=True,
        )
        probe = MagicMock(return_value=True)

        with patch.object(specific, "hwnd_no_longer_exists", probe):
            result = self._strategy(clipboard).insert(
                "hello", _identity_context(0x5555),
            )

        assert result.success is True
        assert result.target_window_gone is False
        probe.assert_not_called()


def _no_prior_credit(handler) -> None:
    """Say that this utterance has credited nothing yet.

    The fixture patches ClipboardOperations, so every counter attribute
    is an auto-created child mock: truthy, and unequal to zero. The
    rebinding guard reads those counters to decide whether the utterance
    already held credited characters, so a test whose scenario is a
    single dictated word has to say so, or the guard reads a credit the
    test never made.
    """
    handler.clipboard.accumulated_paste_chars = 0
    handler.clipboard.accumulated_paste_clusters = 0


def _make_retract_reachable(handler) -> None:
    """Open every retract() gate except the one these tests measure.

    retract() walks several gates before the SimplePaste gate, and each
    one answers not_retracted for its own reason. Only the SimplePaste
    gate belongs to this bead, so the rest are set to the values a
    delivered, verified paste leaves behind. A blocked answer can then
    only come from the gate under test.
    """
    handler.clipboard.accumulated_paste_chars = 5
    handler.clipboard.accumulated_paste_clusters = 5
    handler.clipboard.accumulated_paste_was_qt = False
    handler.clipboard.accumulated_has_grapheme_unsafe = False
    handler.clipboard.last_paste_was_optimistic = False
    handler._user_interacted_during_utterance = False
    # A falsy remembered handle skips the focus-drift gate, which would
    # otherwise call Windows.
    handler.window_manager._last_target_hwnd = 0
    # None takes the legacy back-compat branch, so retract() makes no
    # second capture_context call.
    handler.text_target_predicate = None


class TestTheRetractionGateFollowsTheAttemptThatEndsTheLoop:
    """A6 finding wh-paste-target-window-vanished.1.2.

    The loop runs its per-attempt bookkeeping for every attempt,
    including one the retry is about to supersede. That bookkeeping sets
    self._used_simple_paste on the ClipboardOnlyStrategy branch, and
    that flag is the per-utterance retraction gate: retract() answers
    'simple_paste' for the whole utterance once it is set, and no branch
    ever clears it inside an utterance. So a word the SECOND attempt
    delivered through a strategy that credits the retraction counter
    could never be retracted by voice.

    The accepted shape: the attempt that ENDS the loop governs the flag.
    mark_clipboard_dirty, _last_paste_time and buffer_manager.invalidate()
    still run for BOTH attempts -- that invalidate is what stops the
    retry's TextPerfector composing against the dead window's mirror.

    wh-lost-word-neighbour-paths added a second condition, and that
    condition reads TWO fields. Ending the loop is no longer enough to
    close the gate, but emptiness alone is not enough to leave it open
    either. The attempt must have proved it delivered nothing AND have
    reported its captured window gone.

    delivered_nothing=True proves the attempt put no input in the queue,
    so it has no characters for a retraction to walk back. That fact
    alone was the first shape of this fix, and it was wrong. The handler
    re-binds the remembered window to the newly focused control BEFORE
    it routes, so an empty attempt against a window that is still ALIVE
    leaves the remembered window naming a window nothing was written to,
    while the PREVIOUS window's characters are still on the retraction
    counter. retract() then compares the remembered window against the
    foreground, both name the new window, the focus check passes, and
    the backspaces delete text WheelHouse never wrote. Finding
    wh-lost-word-neighbour-paths.1.2 reported that regression, and the
    first test below is the one whose assertion it moved back.

    Only target_window_gone=True may leave the gate open, and what makes
    that safe is that a dead window can never hold the foreground, so
    the focus check refuses the re-bound case by itself. The two tests
    whose ClipboardOnly attempt DELIVERED still assert the gate closes.

    Every test here drives the real handler into the real retract().
    None of them assigns _used_simple_paste.
    """

    def test_a_first_attempt_against_a_live_window_still_closes_the_gate(
        self, handler,
    ):
        """Case (c): no retry happens, so the first attempt ends the loop.

        A live window that lost the foreground is refused without the
        window-gone report, so the retry never fires. The gate closes.
        That is exactly the behaviour that shipped before
        wh-lost-word-neighbour-paths, and it must not move.

        Part of wh-lost-word-neighbour-paths asserted the opposite here,
        under the name
        test_a_first_attempt_that_proved_nothing_leaves_the_gate_open,
        on the reasoning that an attempt which fired no keystroke wrote
        into no window and so has nothing to walk back. That reasoning
        is incomplete and finding wh-lost-word-neighbour-paths.1.2
        reported it: the window the attempt bound is still alive, the
        handler already re-bound the remembered window to it, and the
        characters on the retraction counter belong to a DIFFERENT
        window. Leaving the gate open here sends that counter's
        backspaces into the live window WheelHouse never wrote to. The
        class docstring above carries the full mechanism.
        """
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_failure(window_gone=False),
        )
        # One spare capture, same program, so a retry that must NOT
        # happen runs for real instead of raising StopIteration -- which
        # the broad except arm would turn into the same False return
        # this test asserts.
        captures = [_context("brave.exe", 0x1111), _context("brave.exe", 0x1111)]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            handler._execute_insert_with_ack("hello", request_id="r1")

        assert capture.call_count == 1
        _make_retract_reachable(handler)

        assert handler.retract() == {
            "status": "not_retracted", "reason": "simple_paste",
        }

    def test_a_first_attempt_that_delivers_still_closes_the_gate(
        self, handler,
    ):
        """Case (c), the delivering half: success ends the loop at once."""
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_DELIVERED,
        )
        captures = [_context("brave.exe", 0x1111), _context("brave.exe", 0x1111)]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            handler._execute_insert_with_ack("hello", request_id="r1")

        assert capture.call_count == 1
        _make_retract_reachable(handler)

        assert handler.retract() == {
            "status": "not_retracted", "reason": "simple_paste",
        }

    def test_a_retry_that_delivers_through_clipboard_only_closes_the_gate(
        self, handler,
    ):
        """Case (a): the retry ends the loop on ClipboardOnlyStrategy.

        The second attempt is the one that ends the loop, and it is a
        ClipboardOnlyStrategy attempt, so the gate must close exactly as
        it does today. A fix that simply stopped the FIRST attempt
        writing the flag would leave this utterance retractable, and
        nothing else in the suite would notice.
        """
        handler.clipboard_only_strategy.insert = MagicMock(side_effect=[
            _clipboard_only_failure(window_gone=True),
            _DELIVERED,
        ])
        _no_prior_credit(handler)
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
            _context("brave.exe", 0x2222),   # spare
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is True
        assert capture.call_count == 2
        _make_retract_reachable(handler)

        assert handler.retract() == {
            "status": "not_retracted", "reason": "simple_paste",
        }

    def test_a_retry_that_fails_and_proved_nothing_leaves_the_gate_open(
        self, handler,
    ):
        """Case (b): the retry ends the loop on ClipboardOnlyStrategy.

        The newly captured window is gone too, so attempt == 1 ends the
        loop and the attempt that ended it was ClipboardOnly.

        Until wh-lost-word-neighbour-paths that closed the gate, and
        this test asserted it. Neither attempt fired a keystroke, so
        neither put a character anywhere; the word failed, and a word
        that reached no window cannot be what a later retraction walks
        back. The gate stays open and the test name moved with the
        assertion.
        """
        handler.clipboard_only_strategy.insert = MagicMock(side_effect=[
            _clipboard_only_failure(window_gone=True),
            _clipboard_only_failure(window_gone=True),
        ])
        _no_prior_credit(handler)
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
            _context("brave.exe", 0x2222),   # spare
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is False
        assert capture.call_count == 2
        _make_retract_reachable(handler)

        with patch(f"{_MOD}.send_backspaces", return_value=True) as backspaces:
            answer = handler.retract()

        assert answer["status"] == "retracted"
        assert answer["chars"] == 5
        backspaces.assert_called_once_with(5)

    def test_a_superseded_attempt_leaves_the_delivered_word_retractable(
        self, handler,
    ):
        """Case (d): the flagship scenario of this whole bead.

        A word dictated at a Brave menu item, the menu destroyed, the
        retry delivering into the window behind it through
        VerifiedUnicodeStrategy -- which credits the retraction counter.
        The superseded ClipboardOnly attempt fired no keystroke and
        credited nothing, so it must not decide the gate.
        """
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_failure(window_gone=True),
        )
        handler.verified_unicode_strategy.insert = MagicMock(
            return_value=_DELIVERED,
        )
        handler.router.get_strategy = MagicMock(side_effect=[
            handler.clipboard_only_strategy,
            handler.verified_unicode_strategy,
        ])
        _no_prior_credit(handler)
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
            _context("brave.exe", 0x2222),   # spare
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is True
        assert capture.call_count == 2
        assert handler.verified_unicode_strategy.insert.call_count == 1
        _make_retract_reachable(handler)

        with patch(f"{_MOD}.send_backspaces", return_value=True) as backspaces:
            answer = handler.retract()

        assert answer["status"] == "retracted"
        assert answer["chars"] == 5
        backspaces.assert_called_once_with(5)

    def test_the_superseded_attempt_keeps_its_clipboard_bookkeeping(
        self, handler,
    ):
        """The fix moves the retraction gate and nothing else.

        The superseded attempt still marks the clipboard dirty, still
        restamps the paste time, and still invalidates the shadow
        buffer. That invalidate is load-bearing: without it the retry's
        TextPerfector composes against the dead window's mirror.
        """
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_failure(window_gone=True),
        )
        handler.verified_unicode_strategy.insert = MagicMock(
            return_value=_DELIVERED,
        )
        handler.router.get_strategy = MagicMock(side_effect=[
            handler.clipboard_only_strategy,
            handler.verified_unicode_strategy,
        ])
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
            _context("brave.exe", 0x2222),   # spare
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            handler._execute_insert_with_ack("hello", request_id="r1")

        assert capture.call_count == 2
        # Both results carry clipboard_dirty=True, so both attempts owe
        # the utterance manager a mark and a restamp.
        assert handler.utterance_manager.mark_clipboard_dirty.call_count == 2
        # Only the ClipboardOnlyStrategy branch invalidates, and only the
        # superseded first attempt took it.
        assert handler.buffer_manager.invalidate.call_count == 1


class TestARetryThatRebindsTheTargetProtectsAnEarlierWord:
    """Codex finding wh-lost-word-neighbour-paths.1.4, boss ruling 9.

    The retry rebinds the remembered target to a newly captured window,
    but the retraction counter is one per-utterance total that records
    nothing about which window each credit went to. When the utterance
    already credited characters before that rebinding, sending the whole
    total to the new window deletes text the user typed there. retract()
    refuses instead of sending the backspaces.
    """

    def test_an_earlier_credited_word_blocks_retraction_after_a_rebinding(
        self, handler,
    ):
        """The harm case, through the default dictation path.

        Word one of this utterance was delivered somewhere and credited
        five characters. Word two loses its captured window, the retry
        moves the target to another window of the same program, and
        VerifiedUnicodeStrategy delivers there. The five characters do
        not belong to the new window, so nothing may be retracted.
        """
        handler.clipboard.accumulated_paste_chars = 5
        handler.clipboard.accumulated_paste_clusters = 5

        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_failure(window_gone=True),
        )
        handler.verified_unicode_strategy.insert = MagicMock(
            return_value=_DELIVERED,
        )
        handler.router.get_strategy = MagicMock(side_effect=[
            handler.clipboard_only_strategy,
            handler.verified_unicode_strategy,
        ])
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
            _context("brave.exe", 0x2222),   # spare
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is True
        assert capture.call_count == 2

        _make_retract_reachable(handler)

        with patch(f"{_MOD}.send_backspaces", return_value=True) as backspaces:
            answer = handler.retract()

        assert answer == {
            "status": "not_retracted", "reason": "retry_rebound_target",
        }
        backspaces.assert_not_called()

    def test_the_clipboard_only_route_is_guarded_the_same_way(
        self, handler,
    ):
        """The route that already existed before this branch.

        A ClipboardOnly retry that fails proves nothing and leaves the
        SimplePaste gate open, so that gate cannot refuse here. The
        rebinding still moved the remembered target, so the earlier
        word's credit would follow the backspaces into the new window.
        The guard names no strategy class, which is what covers this.
        """
        handler.clipboard.accumulated_paste_chars = 5
        handler.clipboard.accumulated_paste_clusters = 5

        handler.clipboard_only_strategy.insert = MagicMock(side_effect=[
            _clipboard_only_failure(window_gone=True),
            _clipboard_only_failure(window_gone=True),
        ])
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
            _context("brave.exe", 0x2222),   # spare
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is False
        assert capture.call_count == 2

        _make_retract_reachable(handler)

        with patch(f"{_MOD}.send_backspaces", return_value=True) as backspaces:
            answer = handler.retract()

        assert answer == {
            "status": "not_retracted", "reason": "retry_rebound_target",
        }
        backspaces.assert_not_called()

    def test_a_rebinding_with_nothing_credited_yet_still_retracts(
        self, handler,
    ):
        """The boundary the guard must not cross.

        When the utterance has credited nothing at the moment of the
        rebinding, every character the user can retract was delivered
        into the window the retry chose. The retraction must still work,
        so the guard must read the counters and not merely the fact that
        a retry happened.
        """
        _no_prior_credit(handler)

        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_failure(window_gone=True),
        )
        handler.verified_unicode_strategy.insert = MagicMock(
            return_value=_DELIVERED,
        )
        handler.router.get_strategy = MagicMock(side_effect=[
            handler.clipboard_only_strategy,
            handler.verified_unicode_strategy,
        ])
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
            _context("brave.exe", 0x2222),   # spare
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            handler._execute_insert_with_ack("hello", request_id="r1")

        _make_retract_reachable(handler)

        with patch(f"{_MOD}.send_backspaces", return_value=True) as backspaces:
            answer = handler.retract()

        assert answer["status"] == "retracted"
        assert answer["chars"] == 5
        backspaces.assert_called_once_with(5)


def _capture_in(hwnd: int) -> UIContext:
    """A capture whose target identity names ``hwnd`` as its window.

    wh-lost-word-neighbour-paths.1.5: the credited-window record reads
    the window through ui.strategies.specific._context_hwnd, which
    prefers the identity a capture already carries. capture_context sets
    one on every real capture, so this is the shape the record meets in
    the app. The _context helper above carries no identity, so reading
    it falls back to resolving the control, and that fallback
    root-normalizes through GetAncestor, which no fabricated handle can
    answer.
    """
    from ui.target_identity import TargetIdentity

    control = MagicMock()
    top_level = MagicMock()
    top_level.NativeWindowHandle = hwnd
    control.GetTopLevelControl.return_value = top_level
    return UIContext(
        focused_control=control,
        is_flutter=False,
        is_terminal=False,
        process_name="brave.exe",
        class_name="Chrome_WidgetWin_1",
        process_id=4242,
        target_identity=TargetIdentity(hwnd=hwnd, root=hwnd),
    )


def _deliver_one_word_into(handler, hwnd: int) -> None:
    """Run one whole word through the handler, delivered into ``hwnd``.

    wh-lost-word-neighbour-paths.1.5: the credited-window set is written
    at the delivery call site, so a test that needs a credited window
    has to run a real delivery. Assigning the counters by hand, the way
    the .1.4 tests do, records no window at all, which is the
    refuse-by-default case and not this one.
    """
    handler.verified_unicode_strategy.insert = MagicMock(
        return_value=_DELIVERED,
    )
    handler.router.get_strategy = MagicMock(
        return_value=handler.verified_unicode_strategy,
    )
    captures = [_capture_in(hwnd)]
    with patch(f"{_MOD}.capture_context", side_effect=captures):
        handler._execute_insert_with_ack("hello", request_id="w")


def _retry_that_lands_on(handler, first: int, second: int) -> bool:
    """Lose the window captured as ``first`` and retry into ``second``.

    Returns whether the word was delivered. The first attempt goes
    through ClipboardOnlyStrategy and reports that its window is gone,
    which is the only failure the retry loop acts on. The retry goes
    through VerifiedUnicodeStrategy and succeeds.
    """
    handler.clipboard_only_strategy.insert = MagicMock(
        return_value=_clipboard_only_failure(window_gone=True),
    )
    handler.verified_unicode_strategy.insert = MagicMock(
        return_value=_DELIVERED,
    )
    handler.router.get_strategy = MagicMock(side_effect=[
        handler.clipboard_only_strategy,
        handler.verified_unicode_strategy,
    ])
    captures = [
        _capture_in(first),
        _capture_in(second),
        _capture_in(second),   # spare
    ]
    with patch(f"{_MOD}.capture_context", side_effect=captures):
        return handler._execute_insert_with_ack("world", request_id="r1")


class TestARetryThatReturnsToTheCreditedWindowKeepsTheRetraction:
    """Codex finding wh-lost-word-neighbour-paths.1.5, boss ruling case A.

    The .1.4 guard refused on the mere fact that a retry re-captured a
    window, because nothing recorded which window the earlier credits
    went to. A transient same-program helper window that dies before any
    keystroke makes the retry capture the ORIGINAL window again, so
    every credit still belongs to the window the backspaces would reach.
    Recording the credited window lets the guard tell that case from a
    retry that really moved.
    """

    def test_a_retry_that_returns_to_the_credited_window_still_retracts(
        self, handler,
    ):
        """The false positive this finding reports.

        Word one is delivered into window A and credited. Word two
        captures a transient same-program window, that window dies
        before any keystroke, and the retry captures A again and
        delivers there. Every credited character sits in A, which is
        also the window the backspaces reach, so the retraction is safe
        and must happen.
        """
        _no_prior_credit(handler)
        _deliver_one_word_into(handler, 0x2222)
        assert handler._credited_target_hwnds == {0x2222}

        handler.clipboard.accumulated_paste_chars = 5
        handler.clipboard.accumulated_paste_clusters = 5

        delivered = _retry_that_lands_on(handler, 0x3333, 0x2222)

        assert delivered is True
        assert handler._retry_rebound_target is False

        _make_retract_reachable(handler)

        with patch(f"{_MOD}.send_backspaces", return_value=True) as backspaces:
            answer = handler.retract()

        assert answer["status"] == "retracted"
        assert answer["chars"] == 5
        backspaces.assert_called_once_with(5)

    def test_a_retry_that_moves_to_a_new_window_still_refuses(
        self, handler,
    ):
        """The harm case .1.4 found, now measured with a real credit.

        Word one is delivered into window A. Word two loses its window
        and the retry lands on a DIFFERENT window B. The credited
        characters are in A, the backspaces would reach B, so the
        refusal must stand.
        """
        _no_prior_credit(handler)
        _deliver_one_word_into(handler, 0x2222)
        assert handler._credited_target_hwnds == {0x2222}

        handler.clipboard.accumulated_paste_chars = 5
        handler.clipboard.accumulated_paste_clusters = 5

        _retry_that_lands_on(handler, 0x3333, 0x4444)

        assert handler._retry_rebound_target is True

        _make_retract_reachable(handler)

        with patch(f"{_MOD}.send_backspaces", return_value=True) as backspaces:
            answer = handler.retract()

        assert answer == {
            "status": "not_retracted", "reason": "retry_rebound_target",
        }
        backspaces.assert_not_called()

    def test_credits_split_across_two_windows_refuse_after_a_retry(
        self, handler,
    ):
        """Term 2 of the ruling: split credits refuse even on a return.

        Two earlier words went to two different windows, so no single
        window holds the whole accumulated total. The retry returns to
        one of them, which is not enough: backspaces sent there would
        still overrun the characters that went to the other window.
        """
        _no_prior_credit(handler)
        _deliver_one_word_into(handler, 0x2222)
        _deliver_one_word_into(handler, 0x4444)
        assert handler._credited_target_hwnds == {0x2222, 0x4444}

        handler.clipboard.accumulated_paste_chars = 5
        handler.clipboard.accumulated_paste_clusters = 5

        _retry_that_lands_on(handler, 0x3333, 0x2222)

        assert handler._retry_rebound_target is True

        _make_retract_reachable(handler)

        with patch(f"{_MOD}.send_backspaces", return_value=True) as backspaces:
            answer = handler.retract()

        assert answer == {
            "status": "not_retracted", "reason": "retry_rebound_target",
        }
        backspaces.assert_not_called()

    def test_a_credit_with_no_recorded_window_refuses_by_default(
        self, handler,
    ):
        """The boss's added check: a missed write site must refuse.

        The credited-window set is written at each delivery call site in
        ui_action_handler. A delivery path that this change does not
        record leaves the set empty while the counter is positive. The
        guard must read that as unknown provenance and refuse, which is
        exactly what it did before this change.
        """
        assert handler._credited_target_hwnds == set()

        handler.clipboard.accumulated_paste_chars = 5
        handler.clipboard.accumulated_paste_clusters = 5

        _retry_that_lands_on(handler, 0x3333, 0x2222)

        assert handler._credited_target_hwnds == {0x2222}
        assert handler._retry_rebound_target is True

        _make_retract_reachable(handler)

        with patch(f"{_MOD}.send_backspaces", return_value=True) as backspaces:
            answer = handler.retract()

        assert answer == {
            "status": "not_retracted", "reason": "retry_rebound_target",
        }
        backspaces.assert_not_called()

    def test_two_windows_without_a_retry_retract_as_they_did_before(
        self, handler,
    ):
        """The boss's scope limit: the guard stays inside the retry.

        An utterance whose words went to two windows with NO retry
        behaves exactly as it did at 1a4299bf. The focus-drift gate at
        ui_action_handler.py:1354-1381 stays the only control there.
        Widening the refusal to every split-window utterance would be a
        behaviour change on the ordinary path, and the ruling does not
        cover it.
        """
        _no_prior_credit(handler)
        _deliver_one_word_into(handler, 0x2222)
        _deliver_one_word_into(handler, 0x4444)

        assert handler._credited_target_hwnds == {0x2222, 0x4444}
        assert handler._retry_rebound_target is False

        _make_retract_reachable(handler)

        with patch(f"{_MOD}.send_backspaces", return_value=True) as backspaces:
            answer = handler.retract()

        assert answer["status"] == "retracted"
        assert answer["chars"] == 5
        backspaces.assert_called_once_with(5)

    def test_both_utterance_boundaries_clear_the_credited_windows(
        self, handler,
    ):
        """Term 1: the new field is cleared beside _retry_rebound_target.

        A window credited in one utterance must not decide the guard in
        the next one, so both boundaries clear it. Without this the set
        would grow across an unbounded number of utterances and the
        split-window refusal would become permanent.
        """
        _no_prior_credit(handler)
        _deliver_one_word_into(handler, 0x2222)
        assert handler._credited_target_hwnds == {0x2222}

        handler.start_utterance(1)
        assert handler._credited_target_hwnds == set()

        _deliver_one_word_into(handler, 0x2222)
        assert handler._credited_target_hwnds == {0x2222}

        handler.end_utterance(1)
        assert handler._credited_target_hwnds == set()
