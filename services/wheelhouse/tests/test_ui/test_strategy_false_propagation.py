"""Strict-callers tests for insertion strategies (wh-d43oi).

Every strategy whose insert() delegates to ClipboardOperations.verified_paste
must propagate a False return rather than silently treating the paste as a
success. A future regression that reassigns to True, ignores the return, or
swallows a failure inside a try/except will fail the corresponding test.

Also covers the wh-r7al.1 composite: UnicodeFirstStrategy must fall back to
StandardStrategy on a Unicode pre-send failure but must NOT fall back when
SendInput already fired.
"""
from unittest.mock import MagicMock, patch

from ui.context import UIContext
from ui.strategies.base import InsertionResult
from ui.strategies.specific import (
    ClipboardFallbackStrategy,
    ShadowBufferStrategy,
    SimplePasteStrategy,
    StandardStrategy,
    UnicodeFirstStrategy,
)


_STRAT_MOD = "ui.strategies.specific"


def _make_context(is_flutter: bool = False) -> UIContext:
    return UIContext(
        focused_control=MagicMock(),
        is_flutter=is_flutter,
        is_terminal=False,
        process_name="notepad.exe",
        class_name="Edit",
        process_id=1234,
    )


def _make_clipboard(paste_returns: bool, last_paste_was_sent: bool = False) -> MagicMock:
    """Build a clipboard mock whose verified_paste mirrors the production
    side effect on ``last_paste_was_sent``.

    Real ClipboardOperations.verified_paste resets the flag to False at
    entry and sets it to True after the Ctrl+V keystroke fires. For tests
    that exercise StandardStrategy's wh-bte fallback gate, the mock must
    reproduce that side effect; otherwise StandardStrategy's pre-call
    reset (which clears stale inter-call state) would make the gate read
    False even when a real verified_paste would have set it True.

    ``last_paste_was_sent`` controls the post-call value the mock leaves
    behind, so tests can pin "shadow's verified_paste fired but the
    post-paste check failed" by passing True.
    """
    clipboard = MagicMock()

    def _fake_verified_paste(*args, **kwargs):
        clipboard.last_paste_was_sent = last_paste_was_sent
        return paste_returns

    clipboard.verified_paste.side_effect = _fake_verified_paste
    clipboard.gather_context.return_value = {
        "preceding_chars": "ab",
        "has_selection": False,
    }
    clipboard.last_paste_was_sent = last_paste_was_sent
    clipboard.last_paste_was_optimistic = False
    return clipboard


def _make_perfector() -> MagicMock:
    perfector = MagicMock()
    perfector.perfected_string.return_value = " hello"
    return perfector


class TestShadowBufferStrategyFalsePropagation:

    def test_false_from_verified_paste_propagates(self):
        buffer = MagicMock()
        buffer.is_valid = True
        buffer.get_context.return_value = {"preceding_chars": "", "has_selection": False}
        clipboard = _make_clipboard(paste_returns=False)
        strategy = ShadowBufferStrategy(buffer, _make_perfector(), clipboard, MagicMock())
        assert strategy.insert("hello", _make_context()).success is False
        # Shadow buffer must NOT be updated when paste failed
        buffer.update_after_insertion.assert_not_called()

    def test_true_from_verified_paste_propagates(self):
        buffer = MagicMock()
        buffer.is_valid = True
        buffer.get_context.return_value = {"preceding_chars": "", "has_selection": False}
        clipboard = _make_clipboard(paste_returns=True)
        strategy = ShadowBufferStrategy(buffer, _make_perfector(), clipboard, MagicMock())
        assert strategy.insert("hello", _make_context()).success is True
        buffer.update_after_insertion.assert_called_once()


class TestClipboardFallbackStrategyFalsePropagation:

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern", return_value=None)
    def test_false_from_verified_paste_propagates(self, _mock_uia):
        buffer = MagicMock()
        clipboard = _make_clipboard(paste_returns=False)
        strategy = ClipboardFallbackStrategy(buffer, _make_perfector(), clipboard, MagicMock())
        assert strategy.insert("hello", _make_context()).success is False
        # Shadow buffer must NOT be updated when paste failed
        buffer.update_from_clipboard_data.assert_not_called()

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern", return_value=None)
    def test_true_from_verified_paste_propagates(self, _mock_uia):
        buffer = MagicMock()
        clipboard = _make_clipboard(paste_returns=True)
        strategy = ClipboardFallbackStrategy(buffer, _make_perfector(), clipboard, MagicMock())
        assert strategy.insert("hello", _make_context()).success is True


class TestStandardStrategyFalsePropagation:

    def test_both_strategies_false_returns_false(self):
        """If both shadow and clipboard fail without sending, StandardStrategy returns False."""
        buffer = MagicMock()
        buffer.is_valid = False
        buffer.synchronize.return_value = False  # Shadow sync fails -> returns False
        # Pre-send sync failure: keystroke never fired, fallback should run.
        clipboard = _make_clipboard(paste_returns=False, last_paste_was_sent=False)
        strategy = StandardStrategy(buffer, _make_perfector(), clipboard, MagicMock())
        with patch(f"{_STRAT_MOD}.read_context_via_text_pattern", return_value=None):
            assert strategy.insert("hello", _make_context()).success is False

    def test_shadow_true_short_circuits(self):
        """When shadow strategy returns True, clipboard fallback is not tried."""
        buffer = MagicMock()
        buffer.is_valid = True
        buffer.get_context.return_value = {"preceding_chars": "", "has_selection": False}
        clipboard = _make_clipboard(paste_returns=True)
        strategy = StandardStrategy(buffer, _make_perfector(), clipboard, MagicMock())
        assert strategy.insert("hello", _make_context()).success is True
        # clipboard.verified_paste called by shadow strategy only, not by fallback
        assert clipboard.verified_paste.call_count == 1


class TestStandardStrategyPostSendNoFallback:
    """Regression: StandardStrategy must NOT fall back to ClipboardFallback
    after a post-send failure (wh-bte). When ShadowBufferStrategy returns
    False but the Ctrl+V keystroke already fired -- e.g. the post-paste
    foreground check failed because the captured target_hwnd was a
    Chromium renderer child while win32gui.GetForegroundWindow returned
    the top-level Chrome_WidgetWin_1 frame -- falling back doubles the
    paste. Symptom in the field: every dictated word appears twice in
    Brave / Claude AI prompt boxes.
    """

    def test_post_send_failure_does_not_call_clipboard_fallback(self):
        """Shadow returns False, last_paste_was_sent=True -> no fallback."""
        buffer = MagicMock()
        buffer.is_valid = True
        buffer.get_context.return_value = {"preceding_chars": "", "has_selection": False}
        # verified_paste returned False (post-paste check) but the Ctrl+V
        # already fired, so last_paste_was_sent is True.
        clipboard = _make_clipboard(paste_returns=False, last_paste_was_sent=True)
        strategy = StandardStrategy(buffer, _make_perfector(), clipboard, MagicMock())

        with patch(f"{_STRAT_MOD}.read_context_via_text_pattern", return_value=None):
            result = strategy.insert("hello", _make_context())

        # Only ONE paste call -- the shadow path. No fallback.
        assert clipboard.verified_paste.call_count == 1
        assert result.success is False

    def test_pre_send_failure_still_falls_back_to_clipboard(self):
        """Shadow returns False, last_paste_was_sent=False -> fallback fires.

        A pre-send failure (clipboard copy failed, or verification rejected
        wrong content before the keystroke) means nothing landed in the
        target. The clipboard fallback path is the legitimate recovery for
        that case and must still run.
        """
        buffer = MagicMock()
        buffer.is_valid = True
        buffer.get_context.return_value = {"preceding_chars": "", "has_selection": False}
        clipboard = _make_clipboard(paste_returns=False, last_paste_was_sent=False)
        strategy = StandardStrategy(buffer, _make_perfector(), clipboard, MagicMock())

        with patch(f"{_STRAT_MOD}.read_context_via_text_pattern", return_value=None):
            strategy.insert("hello", _make_context())

        # Two paste attempts -- shadow + fallback -- because nothing was
        # sent yet.
        assert clipboard.verified_paste.call_count == 2

    def test_shadow_sync_failure_falls_back_when_nothing_sent(self):
        """Shadow sync failure (verified_paste never called) must still
        fall back. This is the existing pre-fix behavior the bug fix
        must preserve."""
        buffer = MagicMock()
        buffer.is_valid = False
        buffer.synchronize.return_value = False  # Shadow strategy returns False before paste
        clipboard = _make_clipboard(paste_returns=True, last_paste_was_sent=False)
        strategy = StandardStrategy(buffer, _make_perfector(), clipboard, MagicMock())

        with patch(f"{_STRAT_MOD}.read_context_via_text_pattern", return_value=None):
            result = strategy.insert("hello", _make_context())

        # Fallback ran and succeeded.
        assert clipboard.verified_paste.call_count == 1
        assert result.success is True


class TestSimplePasteStrategyFalsePropagation:

    def test_false_from_verified_paste_propagates(self):
        clipboard = _make_clipboard(paste_returns=False)
        strategy = SimplePasteStrategy(clipboard, MagicMock())
        assert strategy.insert("hello", _make_context()).success is False

    def test_true_from_verified_paste_propagates(self):
        clipboard = _make_clipboard(paste_returns=True)
        strategy = SimplePasteStrategy(clipboard, MagicMock())
        assert strategy.insert("hello", _make_context()).success is True


class TestUnicodeFirstStrategyFallback:
    """wh-r7al.1: UnicodeFirstStrategy must fall back to StandardStrategy on
    a Unicode pre-send failure but must NOT fall back when SendInput
    already fired (post-send / partial-send failure).
    """

    def _make_unicode_mock(self, success: bool, clipboard_dirty: bool = False):
        unicode_strategy = MagicMock()
        unicode_strategy.insert.return_value = InsertionResult(
            success=success, clipboard_dirty=clipboard_dirty,
        )
        return unicode_strategy

    def _make_standard_mock(self, success: bool = True, clipboard_dirty: bool = True):
        standard_strategy = MagicMock()
        standard_strategy.insert.return_value = InsertionResult(
            success=success, clipboard_dirty=clipboard_dirty,
        )
        return standard_strategy

    def test_unicode_success_returns_unicode_result_no_fallback(self):
        clipboard = MagicMock()
        clipboard.last_paste_was_sent = False
        unicode_strategy = self._make_unicode_mock(success=True, clipboard_dirty=False)
        standard_strategy = self._make_standard_mock()
        composite = UnicodeFirstStrategy(unicode_strategy, standard_strategy, clipboard)

        result = composite.insert("hello", _make_context())

        assert result.success is True
        assert result.clipboard_dirty is False
        unicode_strategy.insert.assert_called_once()
        standard_strategy.insert.assert_not_called()

    def test_unicode_pre_send_failure_falls_back_to_standard(self):
        """SendInput never fired -> safe to retry via StandardStrategy."""
        clipboard = MagicMock()
        clipboard.last_paste_was_sent = False  # no SendInput fired
        unicode_strategy = self._make_unicode_mock(success=False, clipboard_dirty=False)
        standard_strategy = self._make_standard_mock(success=True, clipboard_dirty=True)
        composite = UnicodeFirstStrategy(unicode_strategy, standard_strategy, clipboard)

        result = composite.insert("hello", _make_context())

        assert result.success is True
        assert result.clipboard_dirty is True  # standard wrote the clipboard
        unicode_strategy.insert.assert_called_once()
        standard_strategy.insert.assert_called_once()

    def test_unicode_post_send_failure_does_not_fall_back(self):
        """SendInput fired -> StandardStrategy would double-paste."""
        clipboard = MagicMock()
        clipboard.last_paste_was_sent = True  # partial / post-send Unicode failure
        unicode_strategy = self._make_unicode_mock(success=False, clipboard_dirty=False)
        standard_strategy = self._make_standard_mock()
        composite = UnicodeFirstStrategy(unicode_strategy, standard_strategy, clipboard)

        result = composite.insert("hello", _make_context())

        assert result.success is False
        unicode_strategy.insert.assert_called_once()
        standard_strategy.insert.assert_not_called()

    def test_pre_send_fallback_propagates_standard_failure(self):
        """If both Unicode and Standard fail pre-send, the result is a clean failure."""
        clipboard = MagicMock()
        clipboard.last_paste_was_sent = False
        unicode_strategy = self._make_unicode_mock(success=False, clipboard_dirty=False)
        standard_strategy = self._make_standard_mock(success=False, clipboard_dirty=True)
        composite = UnicodeFirstStrategy(unicode_strategy, standard_strategy, clipboard)

        result = composite.insert("hello", _make_context())

        assert result.success is False
        assert result.clipboard_dirty is True  # Standard touched the clipboard
        standard_strategy.insert.assert_called_once()

    def test_request_id_threaded_to_both_strategies(self):
        """Both inner strategies must see the same request_id for tracing."""
        clipboard = MagicMock()
        clipboard.last_paste_was_sent = False
        unicode_strategy = self._make_unicode_mock(success=False, clipboard_dirty=False)
        standard_strategy = self._make_standard_mock()
        composite = UnicodeFirstStrategy(unicode_strategy, standard_strategy, clipboard)

        ctx = _make_context()
        composite.insert("hello", ctx, request_id="r-trace")

        unicode_strategy.insert.assert_called_once_with("hello", ctx, "r-trace", None)
        standard_strategy.insert.assert_called_once_with("hello", ctx, "r-trace", None)
