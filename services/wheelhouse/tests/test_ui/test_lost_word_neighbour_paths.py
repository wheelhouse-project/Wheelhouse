"""Two neighbours of the vanished-window lost word.

Bead wh-lost-word-neighbour-paths, sibling of
tests/test_ui/test_paste_target_window_vanished.py, which records the
merged ClipboardOnlyStrategy fix and stays as it is.

NEIGHBOUR 1: the default dictation paths lose the word. A captured
window that closes in the instant before the keys are sent makes
UnicodeFirstStrategy or StandardStrategy refuse before any keystroke,
and nothing captures the target again. The merged retry covers only
ClipboardOnlyStrategy.

NEIGHBOUR 2: one word that failed WITH NO KEYSTROKE closes the
per-utterance retraction gate, so retract() refuses the whole utterance
including later words that a counter-crediting strategy delivered.

Both are answered by one new fact on the result:
``InsertionResult.delivered_nothing``. True means the strategy PROVED it
put no input into the operating system queue and wrote nothing into the
target. False means "not proven", so a strategy that never sets it
refuses by default -- no retry, and the retraction gate closes.

The composite rule is the subtle part, and it has its own class below: a
composite must not pass an inner result through unchanged. It reports
True only when EVERY inner attempt it ran proved True.

Every handler test here drives the real
UIActionHandler._execute_insert_with_ack or the real raw_insert_text
with the collaborators mocked, the shape
tests/test_ui/test_paste_target_window_vanished.py uses.
"""
from __future__ import annotations

from dataclasses import replace
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


def _lost_target_context() -> UIContext:
    """A capture whose target identity can never be current.

    ``TargetIdentity()`` is the all-zero record ``ui/context.py``
    captures when the focused control cannot be read. Its
    ``is_current()`` answers False on its first line with no Windows
    call, so a strategy that checks it refuses before any keystroke and
    the test needs no Win32 patching.
    """
    from ui.target_identity import TargetIdentity

    control = MagicMock()
    return UIContext(
        focused_control=control,
        is_flutter=False,
        is_terminal=False,
        process_name="brave.exe",
        class_name="Chrome_WidgetWin_1",
        target_identity=TargetIdentity(),
    )


def _default_path_refusal(
    *, delivered_nothing: bool, window_gone: bool,
) -> InsertionResult:
    """A failed attempt on a default dictation path.

    clipboard_dirty is False because the Unicode path writes no
    clipboard; retry_outcome stays "n/a" because only
    ClipboardOnlyStrategy sets it.
    """
    return InsertionResult(
        success=False,
        clipboard_dirty=False,
        target_window_gone=window_gone,
        delivered_nothing=delivered_nothing,
    )


def _clipboard_only_refusal(
    *, delivered_nothing: bool, window_gone: bool,
) -> InsertionResult:
    """A failed ClipboardOnlyStrategy attempt.

    clipboard_dirty stays True because the clipboard write happens
    before the pre-send proof refuses, so the utterance manager still
    has to restore the user's clipboard.
    """
    return InsertionResult(
        success=False,
        clipboard_dirty=True,
        retry_outcome="unverified",
        target_window_gone=window_gone,
        delivered_nothing=delivered_nothing,
    )


_DELIVERED = InsertionResult(
    success=True, clipboard_dirty=True, retry_outcome="verified",
)

def _stale_rejection_with_the_window_gone() -> InsertionResult:
    """The one shipped shape that reports success AND an empty attempt.

    ``_slow_path_preflight`` builds this object with ``success=True``
    (ui/strategies/specific.py:423) for a focus change it refuses to
    paste through: it is a rejection the user is told about, not a
    failure. ``ClipboardFallbackStrategy`` returns that same object from
    its preflight arm (ui/strategies/specific.py:547) wrapped with
    ``delivered_nothing=True`` and the window probe's answer, and
    ``StandardStrategy`` hands the result straight to the handler. So
    all three of the retry's other conditions can be True at once with
    ``success`` True as well.
    """
    return replace(
        InsertionResult(
            success=True,
            clipboard_dirty=False,
            rejected_reason="stale_focus_changed_to_non_text",
        ),
        delivered_nothing=True,
        target_window_gone=True,
    )


@pytest.fixture
def handler():
    """A UIActionHandler with every collaborator mocked.

    The strategy objects are real, so the handler's isinstance checks
    see the type the router really returns; each test replaces the one
    strategy's ``insert`` it drives.
    """
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
    return built


def _route_to(handler, strategy) -> None:
    """Make the router answer with one strategy for every call."""
    handler.router.get_strategy = MagicMock(return_value=strategy)


def _open_every_gate_but_the_remembered_window(handler) -> None:
    """The retract() state a delivered, verified paste leaves behind.

    Five characters on the counter, no user interaction, no optimistic
    paste, no text-target predicate. Everything except the remembered
    window handle, which each caller sets for itself: the focus-drift
    gate is the one retract() gate that can tell "the empty attempt
    bound the window the counter credits" from "it bound a stranger",
    so a test that measures the SimplePaste gate against a re-bound
    window has to choose a real handle
    (wh-lost-word-neighbour-paths.1.2).
    """
    handler.clipboard.accumulated_paste_chars = 5
    handler.clipboard.accumulated_paste_clusters = 5
    handler.clipboard.accumulated_paste_was_qt = False
    handler.clipboard.accumulated_has_grapheme_unsafe = False
    handler.clipboard.last_paste_was_optimistic = False
    handler._user_interacted_during_utterance = False
    # None takes the legacy back-compat branch, so retract() makes no
    # second capture_context call.
    handler.text_target_predicate = None


def _make_retract_reachable(handler) -> None:
    """Open every retract() gate except the one these tests measure.

    retract() walks several gates before the SimplePaste gate, and each
    one answers not_retracted for its own reason. Only the SimplePaste
    gate belongs to this bead, so the rest are set to the values a
    delivered, verified paste leaves behind. A blocked answer can then
    only come from the gate under test.
    """
    _open_every_gate_but_the_remembered_window(handler)
    # A falsy remembered handle skips the focus-drift gate, which would
    # otherwise call Windows.
    handler.window_manager._last_target_hwnd = 0


def _retract(handler) -> dict:
    """Run the real retract() with the backspace send stubbed out."""
    _make_retract_reachable(handler)
    with patch(f"{_MOD}.send_backspaces", return_value=True):
        return handler.retract()


# --- A1: the default dictation paths stop losing the word ------------------


class TestTheDefaultPathsSurviveADeadTargetWindow:
    """A1: a proven-empty refusal costs one more capture, not the word."""

    def test_a_unicode_first_refusal_that_proved_nothing_is_retried(
        self, handler,
    ):
        handler.unicode_first_strategy.insert = MagicMock(side_effect=[
            _default_path_refusal(delivered_nothing=True, window_gone=True),
            _DELIVERED,
        ])
        _route_to(handler, handler.unicode_first_strategy)
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
        assert handler.unicode_first_strategy.insert.call_count == 2
        handler.response.send_error.assert_not_called()
        handler.response.send_success.assert_called_once()

    def test_a_standard_strategy_refusal_that_proved_nothing_is_retried(
        self, handler,
    ):
        handler.standard_strategy.insert = MagicMock(side_effect=[
            _default_path_refusal(delivered_nothing=True, window_gone=True),
            _DELIVERED,
        ])
        _route_to(handler, handler.standard_strategy)
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is True
        assert capture.call_count == 2
        assert handler.standard_strategy.insert.call_count == 2
        handler.response.send_error.assert_not_called()
        handler.response.send_success.assert_called_once()

    def test_a_default_path_retry_never_crosses_into_another_program(
        self, handler,
    ):
        """Boss ruling option 2 covers the default paths too.

        The word must not land in a different program from the one the
        user spoke it into, whichever strategy refused.
        """
        handler.unicode_first_strategy.insert = MagicMock(
            return_value=_default_path_refusal(
                delivered_nothing=True, window_gone=True,
            ),
        )
        _route_to(handler, handler.unicode_first_strategy)
        captures = [
            _context("brave.exe", 0x1111),
            _context("notepad.exe", 0x3333),
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is False
        assert handler.unicode_first_strategy.insert.call_count == 1
        handler.response.send_error.assert_called_once()


# --- A2: nothing that may have typed is ever retried ------------------------


class TestAnAttemptThatMayHaveTypedIsNeverRetried:
    """A2: "not proven" is the safe default, on every outer type."""

    @pytest.mark.parametrize(
        "attribute",
        ["unicode_first_strategy", "standard_strategy",
         "clipboard_only_strategy"],
    )
    def test_a_refusal_that_proves_nothing_is_never_retried(
        self, handler, attribute,
    ):
        """The window is gone, but the attempt cannot prove it was empty.

        A strategy that already sent input and then failed reports the
        same "window gone" answer, and a retry there types the word a
        second time.
        """
        strategy = getattr(handler, attribute)
        strategy.insert = MagicMock(
            return_value=_clipboard_only_refusal(
                delivered_nothing=False, window_gone=True,
            ),
        )
        _route_to(handler, strategy)
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
        assert strategy.insert.call_count == 1
        handler.response.send_error.assert_called_once()

    def test_a_simple_paste_refusal_is_never_retried(self, handler):
        """SimplePasteStrategy pastes before it verifies anything.

        It is not on the allow-list, and it proves nothing, so both
        guards refuse the retry.
        """
        handler.simple_paste_strategy.insert = MagicMock(
            return_value=_clipboard_only_refusal(
                delivered_nothing=True, window_gone=True,
            ),
        )
        _route_to(handler, handler.simple_paste_strategy)
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
        assert handler.simple_paste_strategy.insert.call_count == 1


class TestASuccessfulRejectionIsNeverRetried:
    """A2's fifth conjunct: ``not result.success`` is a live guard.

    The other four conditions can all be True on a result that also
    reports success. ``_stale_rejection_with_the_window_gone`` above
    names the two production lines that build it. Only
    ``not result.success`` refuses the retry there, and a retry would
    type a word the program has already told the user it REJECTED into
    whichever window the second capture happens to find.
    """

    def test_a_stale_rejection_whose_window_died_is_never_retried(
        self, handler,
    ):
        handler.standard_strategy.insert = MagicMock(
            return_value=_stale_rejection_with_the_window_gone(),
        )
        _route_to(handler, handler.standard_strategy)
        # A spare capture from the same program, so a retry that must
        # not happen runs for real instead of raising StopIteration on
        # an exhausted list. The handler's broad except arm would turn
        # that exception into a refusal, and a short list would let a
        # broken retry pass this test.
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            handler._execute_insert_with_ack("hello", request_id="r1")

        assert capture.call_count == 1
        assert handler.standard_strategy.insert.call_count == 1


class _FailingSelectionClipboard:
    """A ClipboardOperations stand-in whose clear_selection fails.

    ``ClipboardOperations.clear_selection`` sends Ctrl+C and then Delete
    before it can report a failure, so a recorded call here stands for a
    Delete that already reached a window. The paste and the arrow-key
    probe raise, because neither may run after that failure.
    """

    def __init__(self):
        self.last_paste_was_sent = False
        self.last_paste_was_optimistic = False
        self.last_cleared_selection = None
        self.last_clipboard_write_seq = None
        self.clear_selection_calls = 0

    def clear_selection(self, *_args, **_kwargs):
        self.clear_selection_calls += 1
        return False

    def gather_context(self, *_args, **_kwargs):
        raise AssertionError(
            "gather_context must not run after clear_selection failed"
        )

    def verified_paste(self, *_args, **_kwargs):
        raise AssertionError(
            "no paste may follow a failed clear_selection"
        )


def _standard_strategy(clipboard):
    """A real StandardStrategy whose shadow attempt refuses before any send."""
    from ui.strategies.specific import StandardStrategy

    buffer_manager = MagicMock()
    buffer_manager.is_valid = False
    buffer_manager.synchronize.return_value = False
    return StandardStrategy(
        buffer_manager, MagicMock(), clipboard, MagicMock(),
        text_target_predicate=None,
    )


class TestTheClipboardFallbackDeleteIsNeverRepeated:
    """A2's named hazard, driven through the real strategy code."""

    def test_an_abort_after_the_delete_proves_nothing(self):
        """The Delete already reached a window, so nothing is proven.

        ``_abort_before_paste`` runs after clear_selection's Ctrl+C and
        Delete. A result claiming delivered_nothing there would earn the
        word a retry that repeats the Delete.
        """
        from ui.strategies import specific

        clipboard = _FailingSelectionClipboard()
        strategy = _standard_strategy(clipboard)

        with patch.object(
            specific, "read_context_via_text_pattern", return_value=None,
        ):
            result = strategy.insert("hello", _context("brave.exe", 0x1111))

        assert result.success is False
        assert clipboard.clear_selection_calls == 1
        assert result.delivered_nothing is False

    def test_no_second_delete_reaches_a_newly_captured_target(self, handler):
        """The retry gate, not the allow-list, is what stops the Delete.

        StandardStrategy is on the allow-list, so the allow-list alone
        would let this attempt retry. The result is wrapped to report
        the captured window gone -- the worst case, where every other
        retry condition holds -- and the only thing left refusing the
        retry is the unproven delivered_nothing.
        """
        from ui.strategies import specific

        clipboard = _FailingSelectionClipboard()
        strategy = _standard_strategy(clipboard)
        real_insert = strategy.insert

        def insert_reporting_the_window_gone(*args, **kwargs):
            return replace(real_insert(*args, **kwargs), target_window_gone=True)

        strategy.insert = insert_reporting_the_window_gone
        _route_to(handler, strategy)
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
        ]

        with patch.object(
            specific, "read_context_via_text_pattern", return_value=None,
        ), patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is False
        assert capture.call_count == 1
        assert clipboard.clear_selection_calls == 1


# --- A3: one retry per word -------------------------------------------------


class TestTheRetryHappensAtMostOnce:
    """A3: a failed retry on the default path ends and never loops."""

    def test_a_failed_default_path_retry_ends_and_never_loops(self, handler):
        handler.unicode_first_strategy.insert = MagicMock(
            return_value=_default_path_refusal(
                delivered_nothing=True, window_gone=True,
            ),
        )
        _route_to(handler, handler.unicode_first_strategy)
        # A third capture so a third attempt would run for real rather
        # than raise StopIteration, which the broad except arm would
        # turn into the same False return this test asserts.
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
        assert handler.unicode_first_strategy.insert.call_count == 2
        handler.response.send_error.assert_called_once()
        handler.response.send_success.assert_not_called()


# --- The composite rule -----------------------------------------------------


def _unicode_first(verified_unicode, standard, clipboard):
    from ui.strategies.specific import UnicodeFirstStrategy

    return UnicodeFirstStrategy(verified_unicode, standard, clipboard)


def _inner(*, delivered_nothing: bool, clipboard_dirty: bool = True):
    return InsertionResult(
        success=False,
        clipboard_dirty=clipboard_dirty,
        delivered_nothing=delivered_nothing,
    )


class TestACompositeNeverPassesAnInnerAnswerThrough:
    """The boss's added condition on the approved design.

    A composite reports delivered_nothing True only when EVERY inner
    attempt it ran proved True. The dangerous case: the first inner
    attempt sent input, the second proves it delivered nothing, and a
    pass-through reports True for a call that already typed.
    """

    def test_unicode_first_reports_false_when_its_first_attempt_may_have_typed(
        self,
    ):
        clipboard = MagicMock()
        clipboard.last_paste_was_sent = False
        verified = MagicMock()
        verified.insert.return_value = _inner(
            delivered_nothing=False, clipboard_dirty=False,
        )
        standard = MagicMock()
        standard.insert.return_value = _inner(delivered_nothing=True)

        result = _unicode_first(verified, standard, clipboard).insert(
            "hello", _context("brave.exe", 0x1111),
        )

        assert result.delivered_nothing is False
        # Everything else still comes from the attempt that ended the call.
        assert result.clipboard_dirty is True

    def test_unicode_first_reports_true_when_both_attempts_proved_it(self):
        clipboard = MagicMock()
        clipboard.last_paste_was_sent = False
        verified = MagicMock()
        verified.insert.return_value = _inner(
            delivered_nothing=True, clipboard_dirty=False,
        )
        standard = MagicMock()
        standard.insert.return_value = _inner(
            delivered_nothing=True, clipboard_dirty=False,
        )

        result = _unicode_first(verified, standard, clipboard).insert(
            "hello", _context("brave.exe", 0x1111),
        )

        assert result.delivered_nothing is True

    def test_standard_reports_false_when_its_shadow_attempt_may_have_typed(
        self,
    ):
        strategy = _standard_strategy(MagicMock())
        strategy.shadow_strategy.insert = MagicMock(
            return_value=_inner(delivered_nothing=False),
        )
        strategy.clipboard_strategy.insert = MagicMock(
            return_value=_inner(delivered_nothing=True),
        )

        result = strategy.insert("hello", _context("brave.exe", 0x1111))

        assert result.delivered_nothing is False

    def test_standard_reports_true_when_both_attempts_proved_it(self):
        strategy = _standard_strategy(MagicMock())
        strategy.shadow_strategy.insert = MagicMock(
            return_value=_inner(delivered_nothing=True),
        )
        strategy.clipboard_strategy.insert = MagicMock(
            return_value=_inner(delivered_nothing=True),
        )

        result = strategy.insert("hello", _context("brave.exe", 0x1111))

        assert result.delivered_nothing is True

    def test_standard_keeps_the_earlier_clipboard_write_and_the_combination(
        self,
    ):
        """The clipboard_dirty rescue branch carries the combined answer.

        A fallback refusal that wrote no clipboard still inherits the
        shadow attempt's dirty mark. That branch builds its own result
        object, so it is a second place the combination can be lost.
        """
        strategy = _standard_strategy(MagicMock())
        strategy.shadow_strategy.insert = MagicMock(
            return_value=_inner(delivered_nothing=False, clipboard_dirty=True),
        )
        strategy.clipboard_strategy.insert = MagicMock(
            return_value=_inner(delivered_nothing=True, clipboard_dirty=False),
        )

        result = strategy.insert("hello", _context("brave.exe", 0x1111))

        assert result.clipboard_dirty is True
        assert result.delivered_nothing is False


# --- Which branches may prove it -------------------------------------------


class _RefusingClipboard:
    """A ClipboardOperations stand-in whose paste refuses.

    ClipboardOnlyStrategy snapshots and restores four accumulated_*
    fields around verified_paste, so the stand-in has to carry them.
    keystroke_fires decides whether last_paste_was_sent is True when
    verified_paste returns.
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


class TestWhichBranchesProveNothingWasDelivered:
    """Each strategy sets the field only where it can prove it."""

    def test_the_clipboard_fallback_preflight_refusal_proves_it(self):
        """The preflight runs before every keystroke and every write."""
        from ui.strategies.specific import ClipboardFallbackStrategy

        clipboard = _FailingSelectionClipboard()
        strategy = ClipboardFallbackStrategy(
            MagicMock(), MagicMock(), clipboard, MagicMock(),
            text_target_predicate=None,
        )

        result = strategy.insert("hello", _lost_target_context())

        assert result.success is False
        assert result.rejected_reason == "captured_target_lost"
        assert result.delivered_nothing is True
        assert clipboard.clear_selection_calls == 0

    def test_the_verified_unicode_refusal_before_the_send_proves_it(self):
        from ui.strategies.specific import VerifiedUnicodeStrategy

        clipboard = MagicMock()
        strategy = VerifiedUnicodeStrategy(
            MagicMock(), MagicMock(), clipboard, MagicMock(),
        )

        result = strategy.insert("hello", _lost_target_context())

        assert result.success is False
        assert result.rejected_reason == "captured_target_lost"
        assert result.delivered_nothing is True

    def test_the_shadow_buffer_sync_refusal_proves_it(self):
        """The one ShadowBufferStrategy branch that ends before the paste."""
        from ui.strategies.specific import ShadowBufferStrategy

        buffer_manager = MagicMock()
        buffer_manager.is_valid = False
        buffer_manager.synchronize.return_value = False
        clipboard = _FailingSelectionClipboard()
        strategy = ShadowBufferStrategy(
            buffer_manager, MagicMock(), clipboard, MagicMock(),
        )

        result = strategy.insert("hello", _context("brave.exe", 0x1111))

        assert result.success is False
        assert result.delivered_nothing is True

    def test_the_clipboard_only_no_keystroke_refusal_proves_it(self):
        from ui.strategies import specific
        from ui.strategies.specific import ClipboardOnlyStrategy

        clipboard = _RefusingClipboard(keystroke_fires=False)
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=None,
        )

        with patch.object(
            specific, "hwnd_no_longer_exists", MagicMock(return_value=True),
        ):
            result = strategy.insert("hello", _context("brave.exe", 0x5555))

        assert result.success is False
        assert result.target_window_gone is True
        assert result.delivered_nothing is True

    def test_a_clipboard_only_failure_after_the_keystroke_proves_nothing(self):
        from ui.strategies import specific
        from ui.strategies.specific import ClipboardOnlyStrategy

        clipboard = _RefusingClipboard(keystroke_fires=True)
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=None,
        )

        with patch.object(
            specific, "hwnd_no_longer_exists", MagicMock(return_value=True),
        ):
            result = strategy.insert("hello", _context("brave.exe", 0x5555))

        assert result.target_window_gone is False
        assert result.delivered_nothing is False


# --- A4: one empty word must not block the utterance ------------------------


class TestOneEmptyWordDoesNotCloseTheRetractionGate:
    """A4, neighbour 2, through _execute_insert_with_ack."""

    def test_a_clipboard_only_word_that_proved_nothing_leaves_retract_open(
        self, handler,
    ):
        """No keystroke, no credit, no reason to refuse the correction.

        wh-lost-word-neighbour-paths.1.2: the empty attempt's window
        must also be proven GONE, so both attempts here report it gone.
        The first one earns the retry, which means the will_retry guard
        governs it and cannot be what leaves the gate open; the SECOND
        attempt ends the loop with will_retry False, so the exemption
        itself is the only thing that can leave the gate open here.
        """
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_refusal(
                delivered_nothing=True, window_gone=True,
            ),
        )
        _route_to(handler, handler.clipboard_only_strategy)
        # The scenario above is "no credit": this utterance has
        # delivered nothing yet. The fixture's clipboard is a mock, so
        # every counter reads truthy until a test says otherwise, and
        # the rebinding guard (wh-lost-word-neighbour-paths.1.4) reads
        # those counters at the retry.
        handler.clipboard.accumulated_paste_chars = 0
        handler.clipboard.accumulated_paste_clusters = 0
        # Same program on both captures, so the option 2 cross-program
        # refusal does not end the loop before the second attempt runs.
        captures = [
            _context("brave.exe", 0x1111),
            _context("brave.exe", 0x2222),
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            handler._execute_insert_with_ack("hello", request_id="r1")

        assert handler.clipboard_only_strategy.insert.call_count == 2
        answer = _retract(handler)

        assert answer["status"] == "retracted"
        assert answer["chars"] == 5

    def test_a_clipboard_only_word_that_delivered_still_blocks_retract(
        self, handler,
    ):
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_DELIVERED,
        )
        _route_to(handler, handler.clipboard_only_strategy)
        captures = [_context("brave.exe", 0x1111)]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            handler._execute_insert_with_ack("hello", request_id="r1")

        assert _retract(handler) == {
            "status": "not_retracted", "reason": "simple_paste",
        }

    def test_a_simple_paste_word_that_proved_nothing_leaves_retract_open(
        self, handler,
    ):
        """wh-lost-word-neighbour-paths.1.2: empty AND the window gone.

        SimplePasteStrategy is not on the retry allow-list, so
        will_retry is False here whatever the window reports and one
        attempt ends the loop. The exemption is the only thing that can
        leave the gate open.
        """
        handler.simple_paste_strategy.insert = MagicMock(
            return_value=_clipboard_only_refusal(
                delivered_nothing=True, window_gone=True,
            ),
        )
        _route_to(handler, handler.simple_paste_strategy)
        captures = [_context("brave.exe", 0x1111)]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            handler._execute_insert_with_ack("hello", request_id="r1")

        answer = _retract(handler)

        assert answer["status"] == "retracted"
        assert answer["chars"] == 5

    def test_a_simple_paste_word_that_delivered_still_blocks_retract(
        self, handler,
    ):
        handler.simple_paste_strategy.insert = MagicMock(
            return_value=_DELIVERED,
        )
        _route_to(handler, handler.simple_paste_strategy)
        captures = [_context("brave.exe", 0x1111)]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            handler._execute_insert_with_ack("hello", request_id="r1")

        assert _retract(handler) == {
            "status": "not_retracted", "reason": "simple_paste",
        }


def _raw_insert(handler, strategy, result, hwnd: int = 0x1111) -> None:
    """Drive the real raw_insert_text through one strategy result."""
    from ui.ui_action_handler import PasteFailedError

    strategy.insert = MagicMock(return_value=result)
    _route_to(handler, strategy)
    captures = [_context("brave.exe", hwnd)]

    with patch(f"{_MOD}.capture_context", side_effect=captures):
        if result.success:
            handler.raw_insert_text("hello")
        else:
            with pytest.raises(PasteFailedError):
                handler.raw_insert_text("hello")


class TestRawInsertTextFollowsTheSameRule:
    """A4's second half: the parallel block in raw_insert_text."""

    def _run(self, handler, strategy, result):
        _raw_insert(handler, strategy, result)

    def test_a_clipboard_only_raw_insert_that_proved_nothing_leaves_retract_open(
        self, handler,
    ):
        self._run(
            handler, handler.clipboard_only_strategy,
            _clipboard_only_refusal(
                delivered_nothing=True, window_gone=True,
            ),
        )

        answer = _retract(handler)

        assert answer["status"] == "retracted"
        assert answer["chars"] == 5

    def test_a_clipboard_only_raw_insert_that_delivered_blocks_retract(
        self, handler,
    ):
        self._run(handler, handler.clipboard_only_strategy, _DELIVERED)

        assert _retract(handler) == {
            "status": "not_retracted", "reason": "simple_paste",
        }

    def test_a_simple_paste_raw_insert_that_proved_nothing_leaves_retract_open(
        self, handler,
    ):
        self._run(
            handler, handler.simple_paste_strategy,
            _clipboard_only_refusal(
                delivered_nothing=True, window_gone=True,
            ),
        )

        answer = _retract(handler)

        assert answer["status"] == "retracted"
        assert answer["chars"] == 5

    def test_a_simple_paste_raw_insert_that_delivered_blocks_retract(
        self, handler,
    ):
        self._run(handler, handler.simple_paste_strategy, _DELIVERED)

        assert _retract(handler) == {
            "status": "not_retracted", "reason": "simple_paste",
        }


# --- A4's limit: an empty word against a LIVE window ------------------------


class TestAnEmptyWordAgainstALiveWindowStillClosesTheGate:
    """wh-lost-word-neighbour-paths.1.2, the narrowing of A4.

    Proving the attempt was EMPTY is not enough to leave the gate open.
    The handler re-binds the remembered target to the newly focused
    control BEFORE it routes, so after an empty attempt the remembered
    window can name a window this insert never wrote to while the
    characters on the counter were credited in the PREVIOUS window. A
    window that is still alive passes retract's focus-drift gate --
    remembered and foreground both name it -- so nothing else stands
    between "scratch that" and backspaces into the user's own text
    there. The captured window must be proven GONE as well; a dead
    window cannot hold the foreground, so the focus-drift gate refuses
    the re-bound case by itself.
    """

    def test_a_clipboard_only_word_against_a_live_window_closes_the_gate(
        self, handler,
    ):
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_refusal(
                delivered_nothing=True, window_gone=False,
            ),
        )
        _route_to(handler, handler.clipboard_only_strategy)
        captures = [_context("brave.exe", 0x1111)]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            handler._execute_insert_with_ack("hello", request_id="r1")

        assert _retract(handler) == {
            "status": "not_retracted", "reason": "simple_paste",
        }

    def test_a_simple_paste_word_against_a_live_window_closes_the_gate(
        self, handler,
    ):
        handler.simple_paste_strategy.insert = MagicMock(
            return_value=_clipboard_only_refusal(
                delivered_nothing=True, window_gone=False,
            ),
        )
        _route_to(handler, handler.simple_paste_strategy)
        captures = [_context("brave.exe", 0x1111)]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            handler._execute_insert_with_ack("hello", request_id="r1")

        assert _retract(handler) == {
            "status": "not_retracted", "reason": "simple_paste",
        }

    def test_a_clipboard_only_raw_insert_against_a_live_window_closes_it(
        self, handler,
    ):
        _raw_insert(
            handler, handler.clipboard_only_strategy,
            _clipboard_only_refusal(
                delivered_nothing=True, window_gone=False,
            ),
        )

        assert _retract(handler) == {
            "status": "not_retracted", "reason": "simple_paste",
        }

    def test_a_simple_paste_raw_insert_against_a_live_window_closes_it(
        self, handler,
    ):
        _raw_insert(
            handler, handler.simple_paste_strategy,
            _clipboard_only_refusal(
                delivered_nothing=True, window_gone=False,
            ),
        )

        assert _retract(handler) == {
            "status": "not_retracted", "reason": "simple_paste",
        }

    def test_the_rebound_live_window_never_takes_the_other_windows_backspaces(
        self, handler,
    ):
        """The reported sequence, with the real focus-drift gate running.

        Window A (0x1111) took the five characters on the counter
        earlier in the utterance and then closed itself, so focus fell
        on window B (0x2222). The next word captured B, routed to
        ClipboardOnlyStrategy and refused before any keystroke; B is
        alive, so the attempt reports delivered_nothing with the window
        NOT gone, and the handler has already re-bound the remembered
        target to B.

        This test sets no ``_last_target_hwnd = 0`` shortcut. The
        remembered window is B and the foreground is B, so the
        focus-drift gate passes on its own terms and the retraction
        gate is the only thing left: without it, five backspaces delete
        the user's own text in B, where WheelHouse wrote nothing.
        """
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_refusal(
                delivered_nothing=True, window_gone=False,
            ),
        )
        _route_to(handler, handler.clipboard_only_strategy)
        captures = [_context("notepad.exe", 0x2222)]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            handler._execute_insert_with_ack("milk", request_id="r1")

        _open_every_gate_but_the_remembered_window(handler)
        handler.window_manager._last_target_hwnd = 0x2222

        with patch(f"{_MOD}.send_backspaces", return_value=True) as backspaces, \
             patch(f"{_MOD}.win32gui") as win32, \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare",
                 side_effect=lambda h: h or None,
             ):
            win32.GetForegroundWindow.return_value = 0x2222
            answer = handler.retract()

        assert answer == {
            "status": "not_retracted", "reason": "simple_paste",
        }
        backspaces.assert_not_called()


# --- A5: the state after the option 2 refusal -------------------------------


class TestTheOptionTwoRefusalLeavesTheUtteranceRetractable:
    """A5: pin the state the merged code already produces.

    The retry was decided, so the gate stayed open; then the newly
    captured target turned out to belong to another program and the word
    was not delivered. Nothing was written in either attempt, so an
    earlier word of the same utterance must stay retractable.
    """

    def test_the_refused_cross_program_retry_leaves_retract_open(
        self, handler,
    ):
        handler.clipboard_only_strategy.insert = MagicMock(
            return_value=_clipboard_only_refusal(
                delivered_nothing=True, window_gone=True,
            ),
        )
        _route_to(handler, handler.clipboard_only_strategy)
        captures = [
            _context("brave.exe", 0x1111),
            _context("notepad.exe", 0x3333),
        ]

        with patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert delivered is False
        assert capture.call_count == 2
        assert handler.clipboard_only_strategy.insert.call_count == 1
        # A5 read directly, not only through retract(). The refused
        # retry must leave the per-utterance retraction gate UNSET; the
        # retract() answer below can also be produced by other gates, so
        # this assertion is the one that names the state.
        assert handler._used_simple_paste is False
        answer = _retract(handler)

        assert answer["status"] == "retracted"
        assert answer["chars"] == 5


# --- The exclusion that must not be relaxed ---------------------------------


class TestARejectedInsertStillClosesTheGateUnconditionally:
    """The naive reading of A4 is a regression, so pin the exclusion.

    RejectedInsertionStrategy delivers nothing by definition. The gate
    still has to close, because the handler remembered the newly focused
    window BEFORE it routed: the remembered window names a window this
    insert never wrote to, while characters credited in the PREVIOUS
    window are still on the counter, and retracting there would delete
    text the program did not write.
    """

    def _rejection(self) -> InsertionResult:
        return InsertionResult(
            success=True,
            clipboard_dirty=False,
            rejected_reason="default_reject",
            delivered_nothing=True,
        )

    def test_a_rejected_insert_closes_the_gate_although_it_proved_nothing(
        self, handler,
    ):
        handler.rejected_strategy.insert = MagicMock(
            return_value=self._rejection(),
        )
        _route_to(handler, handler.rejected_strategy)
        captures = [_context("brave.exe", 0x1111)]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            handler._execute_insert_with_ack("hello", request_id="r1")

        assert _retract(handler) == {
            "status": "not_retracted", "reason": "simple_paste",
        }

    def test_a_rejected_raw_insert_closes_the_gate_although_it_proved_nothing(
        self, handler,
    ):
        handler.rejected_strategy.insert = MagicMock(
            return_value=self._rejection(),
        )
        _route_to(handler, handler.rejected_strategy)
        captures = [_context("brave.exe", 0x1111)]

        with patch(f"{_MOD}.capture_context", side_effect=captures):
            handler.raw_insert_text("hello")

        assert _retract(handler) == {
            "status": "not_retracted", "reason": "simple_paste",
        }


# --- A1 condition (a): the retry is reachable from a real strategy ---------


def _dead_window_context(hwnd: int) -> UIContext:
    """A capture whose recorded window handle names a destroyed window.

    ``_lost_target_context`` above uses the all-zero ``TargetIdentity``,
    whose ``root`` is 0. ``hwnd_no_longer_exists`` answers False for a
    falsy handle by design, so that context can never prove the window
    is gone. This one records a real handle number in every field, so
    ``is_current()`` reaches its Windows reads and fails on them, and
    the window probe has a handle to ask about.
    """
    from ui.target_identity import TargetIdentity

    return UIContext(
        focused_control=MagicMock(),
        is_flutter=False,
        is_terminal=False,
        process_name="brave.exe",
        class_name="Chrome_WidgetWin_1",
        target_identity=TargetIdentity(
            hwnd=hwnd, root=hwnd, process_id=4242, tag=1,
            root_tag=1, foreground_root=hwnd, foreground_tag=1,
        ),
    )


class TestARealDefaultStrategyReachesTheRetry:
    """A1 condition (a): no hand-built InsertionResult anywhere here.

    Every other test in this file hands the handler a result it built
    itself. That proves what the handler does with the two fields; it
    cannot prove any strategy produces them together, and a design that
    set ``delivered_nothing`` without ``target_window_gone`` passed all
    of them while losing the word exactly as before.

    This test builds a real StandardStrategy, gives it a capture whose
    window is destroyed, and measures the one observable thing the
    retry does: a second capture_context call. The strategy refuses
    both times, so nothing here depends on a working paste.
    """

    def test_a_real_standard_strategy_retries_when_the_window_is_gone(
        self, handler,
    ):
        clipboard = _FailingSelectionClipboard()
        strategy = _standard_strategy(clipboard)
        _route_to(handler, strategy)
        captures = [_dead_window_context(0x1111), _dead_window_context(0x2222)]

        # IsWindow is patched, not hwnd_no_longer_exists, so the probe
        # itself still runs. Removing the probe from the strategy makes
        # this test fail; patching the helper would hide that.
        with patch("ui.hwnd_utils.win32gui.IsWindow", return_value=False), \
             patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert capture.call_count == 2, (
            "the retry never fired: the strategy proved it delivered "
            "nothing, but nothing on this path reported the window gone"
        )
        assert delivered is False
        assert clipboard.clear_selection_calls == 0

    def test_a_real_standard_strategy_does_not_retry_when_the_window_lives(
        self, handler,
    ):
        """The other half: a live window still refuses the retry.

        Without this, a probe hard-coded to True would pass the test
        above. The refusal here comes from the same code path with the
        same capture; only the window's answer differs.
        """
        clipboard = _FailingSelectionClipboard()
        strategy = _standard_strategy(clipboard)
        _route_to(handler, strategy)
        captures = [_dead_window_context(0x1111), _dead_window_context(0x2222)]

        with patch("ui.hwnd_utils.win32gui.IsWindow", return_value=True), \
             patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert capture.call_count == 1
        assert delivered is False
        assert clipboard.clear_selection_calls == 0


# --- A1 condition (b): the buffer-valid path, the state of every word
# --- after a successful one -------------------------------------------------


class _PreSendRefusingClipboard:
    """A ClipboardOperations stand-in whose verified_paste refuses pre-send.

    ``ClipboardOperations.verified_paste`` clears ``last_paste_was_sent``
    at its entry (ui/clipboard_operations.py:687) and returns False on
    the captured-identity check (689-691), which runs before
    ``_safe_copy`` and before the Ctrl+V. The flag is therefore still
    False when that call returns, and that is the proof the strategies
    read. ``keystroke_fires=True`` models the other shape: the Ctrl+V
    went out and a later check failed, so the attempt proves nothing.

    ``clear_selection`` and ``gather_context`` count their calls because
    both send real keys (Ctrl+C, Delete, arrows), and a
    delivered_nothing claim on a branch that ran either of them would be
    false.
    """

    def __init__(self, *, keystroke_fires=False, cleared_selection=None):
        self._keystroke_fires = keystroke_fires
        self.last_paste_was_sent = False
        self.last_paste_was_optimistic = False
        self.last_cleared_selection = cleared_selection
        self.last_clipboard_write_seq = None
        self.paste_calls = 0
        self.clear_selection_calls = 0
        self.gather_context_calls = 0
        self.restore_calls = 0

    def verified_paste(self, *_args, **_kwargs):
        self.paste_calls += 1
        self.last_paste_was_optimistic = False
        self.last_paste_was_sent = self._keystroke_fires
        return False

    def clear_selection(self, *_args, **_kwargs):
        self.clear_selection_calls += 1
        return True

    def gather_context(self, *_args, **_kwargs):
        self.gather_context_calls += 1
        return {"preceding_chars": "", "delivery_failed": False}

    def restore_cleared_selection(self, *_args, **_kwargs):
        """Mirror the real contract: True only when there was text to put back.

        ``ClipboardOperations.restore_cleared_selection``
        (ui/clipboard_operations.py:1332-1379) returns False and does
        nothing when ``last_cleared_selection`` is None. When it is set,
        the method raw-pastes the saved text -- a real Ctrl+V -- and
        then pins ``last_paste_was_sent`` back to its prior value in a
        finally block, which is exactly why that flag cannot report the
        restore.
        """
        self.restore_calls += 1
        if self.last_cleared_selection is None:
            return False
        self.last_cleared_selection = None
        return True


def _valid_buffer_manager():
    """A shadow buffer that needs no sync, the post-first-word state.

    ``ShadowBufferManager.update_after_insertion`` leaves ``is_valid``
    True after every successful word, so this is what the strategy sees
    for every word after the first one in a target that exposes UIA
    TextPattern.
    """
    buffer_manager = MagicMock()
    buffer_manager.is_valid = True
    buffer_manager.get_context.return_value = {}
    return buffer_manager


def _perfector():
    perfector = MagicMock()
    perfector.perfected_string.return_value = "hello "
    return perfector


def _standard_strategy_with_a_valid_buffer(clipboard):
    """A real StandardStrategy whose shadow attempt reaches verified_paste.

    ``_standard_strategy`` above sets ``is_valid = False``, which stops
    the shadow attempt at the synchronize() branch -- the one branch
    that already reported delivered_nothing. This one leaves the buffer
    valid, so the attempt runs through to the post-paste return.
    """
    from ui.strategies.specific import StandardStrategy

    return StandardStrategy(
        _valid_buffer_manager(), _perfector(), clipboard, MagicMock(),
        text_target_predicate=None,
    )


def _unicode_first_over_a_valid_buffer(clipboard):
    """The real UnicodeFirstStrategy the router returns for short text.

    The Unicode attempt refuses at its captured-identity check and
    proves it delivered nothing, so the composite's answer is decided by
    the StandardStrategy attempt underneath it.
    """
    from ui.strategies.specific import (
        UnicodeFirstStrategy,
        VerifiedUnicodeStrategy,
    )

    verified = VerifiedUnicodeStrategy(
        _valid_buffer_manager(), _perfector(), clipboard, MagicMock(),
    )
    return UnicodeFirstStrategy(
        verified, _standard_strategy_with_a_valid_buffer(clipboard), clipboard,
    )


class TestTheBufferValidPathAlsoReachesTheRetry:
    """A1 condition (b): the common mid-utterance state, not the rare one.

    ``TestARealDefaultStrategyReachesTheRetry`` above drives the same
    handler with ``buffer_manager.is_valid = False``, which takes
    ShadowBufferStrategy's synchronize() branch. That branch is the
    minority case: first word of a session, first word after the user
    typed, non-TextPattern targets. With the buffer valid the shadow
    attempt calls verified_paste instead, the refusal comes back from
    inside that call with no keystroke sent, and nothing on the way out
    said so.
    """

    def test_a_real_standard_strategy_retries_when_the_buffer_is_valid(
        self, handler,
    ):
        clipboard = _PreSendRefusingClipboard()
        strategy = _standard_strategy_with_a_valid_buffer(clipboard)
        _route_to(handler, strategy)
        captures = [_dead_window_context(0x1111), _dead_window_context(0x2222)]

        with patch("ui.hwnd_utils.win32gui.IsWindow", return_value=False), \
             patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert capture.call_count == 2, (
            "the retry never fired: the shadow attempt reached "
            "verified_paste, which refused before its Ctrl+V, and the "
            "result never reported the attempt empty"
        )
        assert delivered is False
        assert clipboard.paste_calls == 2
        assert clipboard.clear_selection_calls == 0

    def test_a_real_unicode_first_strategy_retries_when_the_buffer_is_valid(
        self, handler,
    ):
        clipboard = _PreSendRefusingClipboard()
        strategy = _unicode_first_over_a_valid_buffer(clipboard)
        _route_to(handler, strategy)
        captures = [_dead_window_context(0x1111), _dead_window_context(0x2222)]

        with patch("ui.hwnd_utils.win32gui.IsWindow", return_value=False), \
             patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert capture.call_count == 2, (
            "the retry never fired on the short-text default path"
        )
        assert delivered is False
        assert clipboard.paste_calls == 2

    def test_the_composite_reports_both_fields_the_retry_reads(self):
        """The strategy-level answer, with no handler in the way."""
        clipboard = _PreSendRefusingClipboard()
        strategy = _standard_strategy_with_a_valid_buffer(clipboard)

        with patch("ui.hwnd_utils.win32gui.IsWindow", return_value=False):
            result = strategy.insert("hello", _dead_window_context(0x1111))

        assert result.success is False
        assert result.delivered_nothing is True
        assert result.target_window_gone is True

    def test_a_shadow_attempt_whose_ctrl_v_fired_is_never_retried(
        self, handler,
    ):
        """The same path with the keystroke already out the door.

        verified_paste sets ``last_paste_was_sent = True`` immediately
        before it dispatches Ctrl+V (ui/clipboard_operations.py:941), so
        a False return with that flag True means characters may have
        landed. A retry there types the word a second time.

        This is an end-to-end bound on the fix, not a single-cause
        catcher: StandardStrategy returns the shadow result unchanged on
        this path, and that result reports target_window_gone False as
        well, so two of will_retry's conditions refuse the retry. A
        mutation that drops the paste-sent flag from the shadow
        condition still leaves this test green, and
        ``test_a_shadow_buffer_dictation_paste_that_fired_proves_nothing``
        is the test that isolates the flag.
        """
        clipboard = _PreSendRefusingClipboard(keystroke_fires=True)
        strategy = _standard_strategy_with_a_valid_buffer(clipboard)
        _route_to(handler, strategy)
        captures = [_dead_window_context(0x1111), _dead_window_context(0x2222)]

        with patch("ui.hwnd_utils.win32gui.IsWindow", return_value=False), \
             patch(f"{_MOD}.capture_context", side_effect=captures) as capture:
            delivered = handler._execute_insert_with_ack(
                "hello", request_id="r1",
            )

        assert capture.call_count == 1
        assert delivered is False
        assert clipboard.paste_calls == 1


class TestEveryPreSendPasteRefusalProvesItDeliveredNothing:
    """One test per return that can follow a no-keystroke verified_paste.

    The proof is ``last_paste_was_sent`` still False after a False
    return. ``ClipboardFallbackStrategy`` already asks that same
    question for its selection-restore decision
    (ui/strategies/specific.py:651).
    """

    def _shadow(self, clipboard):
        from ui.strategies.specific import ShadowBufferStrategy

        return ShadowBufferStrategy(
            _valid_buffer_manager(), _perfector(), clipboard, MagicMock(),
        )

    def _fallback(self, clipboard):
        from ui.strategies.specific import ClipboardFallbackStrategy

        return ClipboardFallbackStrategy(
            MagicMock(), _perfector(), clipboard, MagicMock(),
            text_target_predicate=None,
        )

    def _verbatim(self):
        from ui.strategies.base import InsertionMode, InsertionOptions

        return InsertionOptions(mode=InsertionMode.VERBATIM)

    def test_the_shadow_buffer_dictation_paste_refusal_proves_it(self):
        clipboard = _PreSendRefusingClipboard()
        strategy = self._shadow(clipboard)

        result = strategy.insert("hello", _context("brave.exe", 0x1111))

        assert result.success is False
        assert result.delivered_nothing is True
        assert strategy.buffer_manager.synchronize.call_count == 0

    def test_a_shadow_buffer_dictation_paste_that_fired_proves_nothing(self):
        clipboard = _PreSendRefusingClipboard(keystroke_fires=True)

        result = self._shadow(clipboard).insert(
            "hello", _context("brave.exe", 0x1111),
        )

        assert result.delivered_nothing is False

    def test_the_shadow_buffer_verbatim_paste_refusal_proves_it(self):
        clipboard = _PreSendRefusingClipboard()

        result = self._shadow(clipboard).insert(
            "hello", _context("brave.exe", 0x1111), options=self._verbatim(),
        )

        assert result.success is False
        assert result.delivered_nothing is True

    def test_a_shadow_buffer_verbatim_paste_that_fired_proves_nothing(self):
        clipboard = _PreSendRefusingClipboard(keystroke_fires=True)

        result = self._shadow(clipboard).insert(
            "hello", _context("brave.exe", 0x1111), options=self._verbatim(),
        )

        assert result.delivered_nothing is False

    def test_the_clipboard_fallback_verbatim_paste_refusal_proves_it(self):
        clipboard = _PreSendRefusingClipboard()

        result = self._fallback(clipboard).insert(
            "hello", _context("brave.exe", 0x1111), options=self._verbatim(),
        )

        assert result.success is False
        assert result.delivered_nothing is True
        assert clipboard.clear_selection_calls == 0

    def test_a_clipboard_fallback_verbatim_paste_that_fired_proves_nothing(
        self,
    ):
        clipboard = _PreSendRefusingClipboard(keystroke_fires=True)

        result = self._fallback(clipboard).insert(
            "hello", _context("brave.exe", 0x1111), options=self._verbatim(),
        )

        assert result.delivered_nothing is False

    def test_the_clipboard_fallback_text_pattern_paste_refusal_proves_it(self):
        """TextPattern read the context, so no key ran before the paste."""
        from ui.strategies import specific

        clipboard = _PreSendRefusingClipboard()

        with patch.object(
            specific, "read_context_via_text_pattern",
            return_value={"preceding_chars": "", "has_selection": False},
        ):
            result = self._fallback(clipboard).insert(
                "hello", _context("brave.exe", 0x1111),
            )

        assert result.success is False
        assert result.delivered_nothing is True
        assert clipboard.clear_selection_calls == 0
        assert clipboard.gather_context_calls == 0

    def test_a_text_pattern_paste_that_fired_the_keystroke_proves_nothing(
        self,
    ):
        """The same sub-branch with the Ctrl+V already dispatched."""
        from ui.strategies import specific

        clipboard = _PreSendRefusingClipboard(keystroke_fires=True)

        with patch.object(
            specific, "read_context_via_text_pattern",
            return_value={"preceding_chars": "", "has_selection": False},
        ):
            result = self._fallback(clipboard).insert(
                "hello", _context("brave.exe", 0x1111),
            )

        assert result.delivered_nothing is False

    def test_the_clipboard_gather_sub_branch_still_proves_nothing(self):
        """clear_selection's Delete and the arrows are real keystrokes.

        ``last_paste_was_sent`` says nothing about them, so this
        sub-branch must keep reporting False however the paste ended.
        """
        from ui.strategies import specific

        clipboard = _PreSendRefusingClipboard()

        with patch.object(
            specific, "read_context_via_text_pattern", return_value=None,
        ):
            result = self._fallback(clipboard).insert(
                "hello", _context("brave.exe", 0x1111),
            )

        assert result.success is False
        assert clipboard.clear_selection_calls == 1
        assert clipboard.gather_context_calls == 1
        assert result.delivered_nothing is False

    def test_a_text_pattern_refusal_that_restored_a_selection_proves_nothing(
        self,
    ):
        """The restore raw-pastes, and the flag is pinned back over it.

        ``restore_cleared_selection`` sends a real Ctrl+V when an
        earlier call left text in ``last_cleared_selection``, then
        resets ``last_paste_was_sent`` to its prior value in a finally
        block. The paste-sent flag alone would report this attempt as
        empty.
        """
        from ui.strategies import specific

        clipboard = _PreSendRefusingClipboard(cleared_selection="earlier")

        with patch.object(
            specific, "read_context_via_text_pattern",
            return_value={"preceding_chars": "", "has_selection": False},
        ):
            result = self._fallback(clipboard).insert(
                "hello", _context("brave.exe", 0x1111),
            )

        assert clipboard.restore_calls == 1
        assert result.delivered_nothing is False

    def test_the_simple_paste_refusal_before_the_send_proves_it(self):
        from ui.strategies.specific import SimplePasteStrategy

        clipboard = _PreSendRefusingClipboard()
        strategy = SimplePasteStrategy(clipboard, MagicMock())

        result = strategy.insert("hello", _context("brave.exe", 0x1111))

        assert result.success is False
        assert result.delivered_nothing is True

    def test_a_simple_paste_that_fired_the_keystroke_proves_nothing(self):
        from ui.strategies.specific import SimplePasteStrategy

        clipboard = _PreSendRefusingClipboard(keystroke_fires=True)
        strategy = SimplePasteStrategy(clipboard, MagicMock())

        result = strategy.insert("hello", _context("brave.exe", 0x1111))

        assert result.delivered_nothing is False
