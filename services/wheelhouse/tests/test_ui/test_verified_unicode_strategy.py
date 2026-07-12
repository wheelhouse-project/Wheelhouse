"""Tests for VerifiedUnicodeStrategy (wh-9jml6).

The Unicode strategy must reuse the StandardStrategy composition pipeline:
TextPerfector for spacing/capitalization, shadow buffer context lookup
before send, shadow buffer update after success, retraction counter
update on success only, and a post-send foreground check matching
verified_paste's wh-oe7u.3 behavior. A standalone path that skipped
those would corrupt streamed dictation output and retraction accounting.

Tests use real TextPerfector and ShadowBufferManager so the
multi-word streamed dictation path is exercised end-to-end. The Win32
boundary (type_string_verified, win32gui.GetForegroundWindow,
ensure_focused) is patched.
"""
from unittest.mock import MagicMock, patch

import pytest

from ui.context import UIContext
from ui.shadow_buffer import ShadowBufferManager
from ui.text_perfector import TextPerfector
from ui.strategies.specific import VerifiedUnicodeStrategy


_STRAT_MOD = "ui.strategies.specific"


def _make_focused_control(hwnd: int = 4242):
    """Build a mock UIA control whose top-level window has the given HWND."""
    top_level = MagicMock()
    top_level.NativeWindowHandle = hwnd
    control = MagicMock()
    control.GetTopLevelControl.return_value = top_level
    return control


def _make_context(hwnd: int = 4242, is_flutter: bool = False) -> UIContext:
    return UIContext(
        focused_control=_make_focused_control(hwnd),
        is_flutter=is_flutter,
        is_terminal=False,
        process_name="notepad.exe",
        class_name="Edit",
        process_id=1234,
    )


def _make_clipboard_ops() -> MagicMock:
    """A mock clipboard_ops whose retraction-counter fields behave like the real one.

    wh-pkhrp.3.6: ``credit_paste_chars`` is the canonical hook the
    real strategies call. The mock implements it as a thin shim so
    tests that read ``accumulated_paste_chars`` after a successful
    paste see the same field semantics they did when the strategies
    mutated the counter inline.
    """
    clipboard = MagicMock()
    clipboard.accumulated_paste_chars = 0
    clipboard.accumulated_has_grapheme_unsafe = False
    # wh-pkhrp.2: cluster counter and Qt-target sticky flag.
    clipboard.accumulated_paste_clusters = 0
    clipboard.accumulated_paste_was_qt = False
    clipboard.last_paste_was_optimistic = False
    clipboard.last_paste_was_sent = False

    def _credit(text: str, target_class_name: str = "") -> None:
        if not text:
            return
        clipboard.accumulated_paste_chars += len(text)
        # Cluster counter mirror: ASCII case counts the same as len.
        # The strategy-level tests do not exercise grapheme-cluster
        # cases through this mock, so simple per-code-point advance is
        # adequate for parity with the production behavior the tests
        # observe.
        clipboard.accumulated_paste_clusters += len(text)
        # Match ClipboardOperations.text_contains_grapheme_unsafe_chars
        # without importing it (avoids circular test dependency).
        for ch in text:
            cp = ord(ch)
            if cp >= 0x10000 or cp == 0x200D:
                clipboard.accumulated_has_grapheme_unsafe = True
                break
        # Mirror the Qt-target sticky flag by delegating to the
        # production helper. Reimplementing the check inline (wh-pkhrp.2.2.2
        # deepseek finding) would drift if the production rule is
        # refined; delegate so the test stays honest.
        from services.wheelhouse.ui.clipboard_operations import ClipboardOperations
        if ClipboardOperations.is_qt_class_name(target_class_name):
            clipboard.accumulated_paste_was_qt = True

    clipboard.credit_paste_chars = _credit

    def _grapheme_unsafe(text: str) -> bool:
        if not text:
            return False
        return any(ord(ch) >= 0x10000 or ord(ch) == 0x200D for ch in text)

    clipboard.text_contains_grapheme_unsafe_chars = _grapheme_unsafe
    return clipboard


@pytest.fixture
def buffer_manager():
    """A real ShadowBufferManager pre-seeded as if a UIA sync had run.

    Set _cursor_pos != -1 so is_valid returns True without an actual
    UIA call. Subsequent update_after_insertion calls then exercise
    the real composition logic.
    """
    bm = ShadowBufferManager()
    bm._buffer = ""
    bm._cursor_pos = 0
    bm._selection_len = 0
    return bm


@pytest.fixture
def text_perfector():
    return TextPerfector()


@pytest.fixture
def window_manager():
    wm = MagicMock()
    wm.ensure_focused.return_value = True
    return wm


def _patched_normalize(hwnd):
    """Identity normalize -- skip win32 GetAncestor in tests."""
    if hwnd is None:
        return None
    return int(hwnd)


class TestVerifiedUnicodeStrategySuccess:
    """Full-success path: focus held, every event sent, counter and shadow updated."""

    def test_success_updates_shadow_and_counter(
        self, buffer_manager, text_perfector, window_manager
    ):
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            return_value=(True, len("Hello"), None),
        ) as mock_send, patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            result = strategy.insert("hello", context)

        assert result.success is True
        # The composed string ("Hello") was sent, not the raw word.
        mock_send.assert_called_once_with("Hello")
        # Counter incremented by perfected length, not raw input length.
        assert clipboard.accumulated_paste_chars == len("Hello")
        # Shadow buffer reflects the inserted perfected text.
        assert buffer_manager._buffer == "Hello"
        assert buffer_manager._cursor_pos == 5

    def test_qt_class_name_threads_through_to_was_qt_flag(
        self, buffer_manager, text_perfector, window_manager
    ):
        """wh-pkhrp.2.2.2 (deepseek finding): strategy.insert with a
        Qt-classed context must thread context.class_name to
        credit_paste_chars and flip accumulated_paste_was_qt to True.
        Earlier tests only used class_name="Edit" so the threading was
        never exercised end-to-end. A refactor that drops the kwarg or
        renames it would silently regress this without the test.
        """
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        qt_context = UIContext(
            focused_control=_make_focused_control(4242),
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="QPlainTextEdit",
            process_id=1234,
        )
        assert clipboard.accumulated_paste_was_qt is False

        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            return_value=(True, len("Hello"), None),
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            result = strategy.insert("hello", qt_context)

        assert result.success is True
        assert clipboard.accumulated_paste_was_qt is True

    def test_non_qt_class_name_leaves_was_qt_flag_false(
        self, buffer_manager, text_perfector, window_manager
    ):
        """Same threading exercised with a non-Qt class name -- the
        flag must stay False. Paired with the Qt test above so a
        future regression that always sets the flag is caught."""
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)  # class_name="Edit"
        assert clipboard.accumulated_paste_was_qt is False

        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            return_value=(True, len("Hello"), None),
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            result = strategy.insert("hello", context)

        assert result.success is True
        assert clipboard.accumulated_paste_was_qt is False

    def test_streamed_dictation_capitalizes_and_spaces_correctly(
        self, buffer_manager, text_perfector, window_manager
    ):
        """`hello` then `world` must produce `Hello world` with one space."""
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        sent_strings: list[str] = []

        def fake_send(text):
            sent_strings.append(text)
            return (True, len(text), None)

        with patch(
            f"{_STRAT_MOD}.type_string_verified", side_effect=fake_send
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            assert strategy.insert("hello", context).success is True
            assert strategy.insert("world", context).success is True

        assert sent_strings == ["Hello", " world"]
        # Accumulated counter sums both perfected lengths.
        assert clipboard.accumulated_paste_chars == len("Hello") + len(" world")
        assert buffer_manager._buffer == "Hello world"

    def test_focus_restored_before_send(
        self, buffer_manager, text_perfector, window_manager
    ):
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)
        target_control = context.focused_control

        with patch(
            f"{_STRAT_MOD}.type_string_verified", return_value=(True, 5, None)
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            strategy.insert("hello", context)

        window_manager.ensure_focused.assert_called_once_with(4242)
        target_control.SetFocus.assert_called_once()

    def test_provenance_flags_set_for_retraction_gate(
        self, buffer_manager, text_perfector, window_manager
    ):
        """last_paste_was_sent True after success; last_paste_was_optimistic False.

        Retraction reads last_paste_was_optimistic to gate paste_unverified.
        Unicode delivery is never optimistic (no clipboard verification),
        so it must always be False after a successful send.
        """
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified", return_value=(True, 5, None)
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            strategy.insert("hello", context)

        assert clipboard.last_paste_was_sent is True
        assert clipboard.last_paste_was_optimistic is False


class TestVerifiedUnicodeStrategyPartialSend:
    """Partial-send path: SendInput accepted some events, returns (False, n, ...)."""

    def test_partial_send_does_not_credit_counter_and_invalidates_buffer(
        self, buffer_manager, text_perfector, window_manager
    ):
        """Partial send does not increment accumulated_paste_chars and
        invalidates the shadow buffer (wh-0juh.2) so the next compose
        re-syncs against the actual UI state instead of the stale cached
        text from before the partial send."""
        clipboard = _make_clipboard_ops()
        # Pre-seed buffer so we can prove invalidate() was called.
        buffer_manager._buffer = "xx"
        buffer_manager._cursor_pos = 2

        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            return_value=(False, 3, "partial: expected 12 got 6 at chunk offset 0"),
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            result = strategy.insert("hello", context)

        assert result.success is False
        assert clipboard.accumulated_paste_chars == 0
        # Buffer invalidated -- next insertion will re-sync against the
        # actual field rather than reading stale "xx".
        assert buffer_manager.is_valid is False

    def test_partial_send_marks_paste_was_sent(
        self, buffer_manager, text_perfector, window_manager
    ):
        """Partial send still flips last_paste_was_sent so a downstream
        StandardStrategy fallback (if ever wired) would not double-paste."""
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            return_value=(False, 2, "partial: expected 10 got 4 at chunk offset 0"),
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            strategy.insert("hello", context)

        assert clipboard.last_paste_was_sent is True


class TestVerifiedUnicodeStrategyWin32Failure:
    """Total Win32 failure: SendInput returned 0, error code surfaced."""

    def test_win32_failure_returns_false(
        self, buffer_manager, text_perfector, window_manager
    ):
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            return_value=(False, 0, "win32 error 5"),
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            result = strategy.insert("hello", context)

        assert result.success is False
        assert clipboard.accumulated_paste_chars == 0

    def test_send_exception_returns_false(
        self, buffer_manager, text_perfector, window_manager
    ):
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            side_effect=RuntimeError("ctypes blew up"),
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            result = strategy.insert("hello", context)

        assert result.success is False
        assert clipboard.accumulated_paste_chars == 0


class TestVerifiedUnicodeStrategyFocusDrift:
    """Post-send foreground check (wh-oe7u.3 parity)."""

    def test_chromium_helper_window_same_process_credits(
        self, buffer_manager, text_perfector, window_manager
    ):
        """wh-3nwy: a transient Chromium helper window briefly owns
        foreground after the paste. Different root HWND, SAME process
        as the captured target. The post-send check accepts via the
        same-process fallback rather than rejecting (the false-positive
        failure that wh-3nwy fixes).
        """
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        # Captured target is the Brave main HWND.
        context = _make_context(hwnd=4201052)

        def _pid_by_hwnd(hwnd):
            # Both HWNDs (target main and observed helper) are
            # owned by the same brave.exe process -- PID 8888.
            return (1234, 8888)

        # Fake psutil.Process(pid).name() to report brave.exe so the
        # strategy's wh-ix1z.19 process-name scope opt-in fires.
        fake_proc = type("FakeProc", (), {"name": lambda self: "brave.exe"})()

        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            return_value=(True, len("Hello"), None),
        ), patch(
            # Foreground is now the autocomplete popup (different root).
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=1573546
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.win32process.GetWindowThreadProcessId",
            side_effect=_pid_by_hwnd,
        ), patch(
            "ui.hwnd_utils.psutil.Process", return_value=fake_proc,
        ):
            result = strategy.insert("hello", context)

        # Same-process fallback accepted the paste.
        assert result.success is True
        # Counter credited.
        assert clipboard.accumulated_paste_chars == len("Hello")

    def test_non_browser_same_process_drift_still_rejects(
        self, buffer_manager, text_perfector, window_manager
    ):
        """wh-ix1z.19: the same-process fallback is opt-in by exe name.

        For a NON-browser app (Word, Outlook, Visual Studio, etc.) the
        strict GA_ROOT-only behavior is preserved -- a same-process
        focus shift to a sibling top-level window (a Word dialog,
        Visual Studio popup) is treated as a real drift and rejects.
        """
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        def _pid_by_hwnd(hwnd):
            # Both HWNDs in the same Word process -- but Word is NOT
            # on the browser allowlist, so the same-process fallback
            # does NOT activate.
            return (1234, 9999)

        fake_proc = type("FakeProc", (), {"name": lambda self: "winword.exe"})()

        with patch(
            f"{_STRAT_MOD}.type_string_verified", return_value=(True, 5, None)
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=8888
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.win32process.GetWindowThreadProcessId",
            side_effect=_pid_by_hwnd,
        ), patch(
            "ui.hwnd_utils.psutil.Process", return_value=fake_proc,
        ):
            result = strategy.insert("hello", context)

        # Strict GA_ROOT mismatch rejects even though both HWNDs are
        # in the same process -- winword.exe is not on the
        # browser allowlist.
        assert result.success is False
        assert clipboard.accumulated_paste_chars == 0

    def test_post_send_foreground_mismatch_returns_false(
        self, buffer_manager, text_perfector, window_manager
    ):
        """Focus drifted between strategy entry and post-send check.

        wh-3nwy: the post-send check now uses
        hwnds_match_for_foreground_compare with allow_same_process=True.
        For a true cross-process drift the same-process fallback must
        also fail -- patch GetWindowThreadProcessId so the two HWNDs
        report DIFFERENT PIDs.
        """
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        def _pid_by_hwnd(hwnd):
            # Cross-process drift: target HWND -> PID 1, observed -> PID 2.
            return (1234, 1 if hwnd == 4242 else 2)

        with patch(
            f"{_STRAT_MOD}.type_string_verified", return_value=(True, 5, None)
        ), patch(
            # Foreground is now a different window after send.
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=9999
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.win32process.GetWindowThreadProcessId",
            side_effect=_pid_by_hwnd,
        ):
            result = strategy.insert("hello", context)

        assert result.success is False
        # Counter NOT credited because the paste did not land on the
        # captured target.
        assert clipboard.accumulated_paste_chars == 0
        # Shadow buffer NOT updated.
        assert buffer_manager._buffer == ""

    def test_observed_hwnd_normalize_failure_returns_false(
        self, buffer_manager, text_perfector, window_manager
    ):
        """If GetForegroundWindow's HWND fails normalization, fail closed.

        wh-ix1z.20: must patch ui.hwnd_utils.normalize_hwnd_for_foreground_compare
        because the post-send check goes through hwnds_match_for_foreground_compare,
        whose internal call references the helper at its own module path.
        """
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        def normalize_only_target(hwnd):
            if hwnd == 4242:
                return 4242
            return None  # Observed side fails to normalize.

        with patch(
            f"{_STRAT_MOD}.type_string_verified", return_value=(True, 5, None)
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=9999
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=normalize_only_target,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=normalize_only_target,
        ):
            result = strategy.insert("hello", context)

        assert result.success is False
        assert clipboard.accumulated_paste_chars == 0

    def test_target_hwnd_normalize_failure_returns_false(
        self, buffer_manager, text_perfector, window_manager
    ):
        """If target_hwnd cannot be normalized, fail closed before crediting counter.

        wh-ix1z.20: patch BOTH module paths -- the strategy module's
        reference (consulted by _hwnd_from_control before the post-send
        check) AND the helper module's reference (consulted by
        hwnds_match_for_foreground_compare).
        """
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified", return_value=(True, 5, None)
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            return_value=None,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            return_value=None,
        ):
            result = strategy.insert("hello", context)

        assert result.success is False
        assert clipboard.accumulated_paste_chars == 0

    def test_get_foreground_window_exception_returns_false(
        self, buffer_manager, text_perfector, window_manager
    ):
        """GetForegroundWindow raising fails closed -- no counter credit."""
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified", return_value=(True, 5, None)
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow",
            side_effect=OSError("boom"),
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            result = strategy.insert("hello", context)

        assert result.success is False
        assert clipboard.accumulated_paste_chars == 0


class TestVerifiedUnicodeStrategyTargetMissing:
    """No focused_control / no resolvable HWND must fail closed."""

    def test_no_focused_control_returns_false(
        self, buffer_manager, text_perfector, window_manager
    ):
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = UIContext(
            focused_control=None,
            is_flutter=False,
            is_terminal=False,
            process_name="notepad.exe",
            class_name="Edit",
            process_id=1234,
        )

        with patch(
            f"{_STRAT_MOD}.type_string_verified", return_value=(True, 5, None)
        ) as mock_send:
            result = strategy.insert("hello", context)

        assert result.success is False
        # Send must not have fired without a target.
        mock_send.assert_not_called()
        assert clipboard.accumulated_paste_chars == 0


class TestVerifiedUnicodeStrategyBufferSync:
    """Shadow buffer must be valid (or syncable) before composing."""

    def test_invalid_buffer_that_cannot_sync_returns_false(
        self, text_perfector, window_manager
    ):
        bm = MagicMock()
        bm.is_valid = False
        bm.synchronize.return_value = False
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            bm, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified", return_value=(True, 5, None)
        ) as mock_send:
            result = strategy.insert("hello", context)

        assert result.success is False
        mock_send.assert_not_called()
        # update_after_insertion was never reached.
        bm.update_after_insertion.assert_not_called()


class TestVerifiedUnicodeStrategyProvenanceReset:
    """Each insert() resets the paste provenance flags so prior state cannot leak."""

    def test_prior_optimistic_flag_cleared_on_entry(
        self, buffer_manager, text_perfector, window_manager
    ):
        clipboard = _make_clipboard_ops()
        # Simulate a prior verified_paste having set this.
        clipboard.last_paste_was_optimistic = True

        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified", return_value=(True, 5, None)
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            strategy.insert("hello", context)

        assert clipboard.last_paste_was_optimistic is False


class TestVerifiedUnicodeStrategyPoisonRetract:
    """wh-0juh.1: a failure after last_paste_was_sent=True must poison retract.

    Within an utterance, an earlier successful insertion leaves
    accumulated_paste_chars > 0. A later partial send or post-send
    foreground mismatch must NOT leave that prior credit retractable --
    backspaces would walk over uncredited partially-landed text and
    delete the wrong span. The strategy sets last_paste_was_optimistic
    = True on every post-send failure path; the retraction gate at
    ui_action_handler.retract refuses with reason='paste_unverified'.
    """

    def _run_failure_scenario(
        self, buffer_manager, text_perfector, window_manager,
        send_outcome, foreground_outcome, normalize_outcome=_patched_normalize,
    ):
        """Run strategy.insert with the given outcomes and return clipboard state."""
        clipboard = _make_clipboard_ops()
        # Prior word in the same utterance succeeded.
        clipboard.accumulated_paste_chars = 5

        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        if isinstance(send_outcome, BaseException):
            send_kwargs = {"side_effect": send_outcome}
        else:
            send_kwargs = {"return_value": send_outcome}

        if isinstance(foreground_outcome, BaseException):
            fg_kwargs = {"side_effect": foreground_outcome}
        else:
            fg_kwargs = {"return_value": foreground_outcome}

        # wh-ix1z.20: patch BOTH module paths so the post-send check's
        # internal call (via hwnds_match_for_foreground_compare) hits
        # the same normalize stub as the strategy module's reference.
        with patch(
            f"{_STRAT_MOD}.type_string_verified", **send_kwargs
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", **fg_kwargs
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=normalize_outcome,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=normalize_outcome,
        ):
            result = strategy.insert("world", context)

        return result, clipboard

    def test_partial_send_poisons_retract_and_invalidates_buffer(
        self, buffer_manager, text_perfector, window_manager
    ):
        result, clipboard = self._run_failure_scenario(
            buffer_manager, text_perfector, window_manager,
            send_outcome=(False, 3, "partial: expected 12 got 6 at chunk offset 0"),
            foreground_outcome=4242,
        )
        assert result.success is False
        # Prior credit untouched -- not zeroed -- but retract is now blocked.
        assert clipboard.accumulated_paste_chars == 5
        assert clipboard.last_paste_was_optimistic is True
        # Buffer invalidated -- next insertion will re-sync.
        assert buffer_manager.is_valid is False

    def test_win32_failure_poisons_retract_and_invalidates_buffer(
        self, buffer_manager, text_perfector, window_manager
    ):
        result, clipboard = self._run_failure_scenario(
            buffer_manager, text_perfector, window_manager,
            send_outcome=(False, 0, "win32 error 5"),
            foreground_outcome=4242,
        )
        assert result.success is False
        assert clipboard.last_paste_was_optimistic is True
        assert buffer_manager.is_valid is False

    def test_send_exception_poisons_retract_and_invalidates_buffer(
        self, buffer_manager, text_perfector, window_manager
    ):
        result, clipboard = self._run_failure_scenario(
            buffer_manager, text_perfector, window_manager,
            send_outcome=RuntimeError("ctypes blew up"),
            foreground_outcome=4242,
        )
        assert result.success is False
        assert clipboard.last_paste_was_optimistic is True
        assert buffer_manager.is_valid is False

    def test_post_send_foreground_mismatch_poisons_retract(
        self, buffer_manager, text_perfector, window_manager
    ):
        result, clipboard = self._run_failure_scenario(
            buffer_manager, text_perfector, window_manager,
            send_outcome=(True, 6, None),
            foreground_outcome=9999,  # Different window in foreground.
        )
        assert result.success is False
        assert clipboard.last_paste_was_optimistic is True
        assert buffer_manager.is_valid is False

    def test_get_foreground_window_exception_poisons_retract(
        self, buffer_manager, text_perfector, window_manager
    ):
        result, clipboard = self._run_failure_scenario(
            buffer_manager, text_perfector, window_manager,
            send_outcome=(True, 6, None),
            foreground_outcome=OSError("boom"),
        )
        assert result.success is False
        assert clipboard.last_paste_was_optimistic is True
        assert buffer_manager.is_valid is False

    def test_target_hwnd_normalize_failure_poisons_retract(
        self, buffer_manager, text_perfector, window_manager
    ):
        """target_hwnd normalization succeeds at strategy entry but fails
        the second normalize call after the send (e.g. window destroyed
        mid-paste). The post-send branch must still poison retract."""
        # Two-step normalize: first call (in _hwnd_from_control at entry)
        # succeeds, second call (post-send re-normalize for expected_root)
        # fails. Subsequent observed-side calls still pass so the failure
        # is unambiguously the target side.
        call_state = {"count": 0}

        def normalize_first_pass_then_fail(hwnd):
            call_state["count"] += 1
            if call_state["count"] == 1:
                return int(hwnd) if hwnd is not None else None
            return None

        result, clipboard = self._run_failure_scenario(
            buffer_manager, text_perfector, window_manager,
            send_outcome=(True, 6, None),
            foreground_outcome=4242,
            normalize_outcome=normalize_first_pass_then_fail,
        )
        assert result.success is False
        assert clipboard.last_paste_was_optimistic is True
        assert buffer_manager.is_valid is False

    def test_observed_hwnd_normalize_failure_poisons_retract(
        self, buffer_manager, text_perfector, window_manager
    ):
        """observed foreground HWND normalization fails after a successful send."""
        # Normalize target (4242) ok, but anything else (the foreground)
        # fails to normalize.
        def normalize_only_target(hwnd):
            if hwnd == 4242:
                return 4242
            return None

        result, clipboard = self._run_failure_scenario(
            buffer_manager, text_perfector, window_manager,
            send_outcome=(True, 6, None),
            foreground_outcome=9999,
            normalize_outcome=normalize_only_target,
        )
        assert result.success is False
        assert clipboard.last_paste_was_optimistic is True
        assert buffer_manager.is_valid is False


class TestVerifiedUnicodeStrategyPreSendFailuresDoNotPoison:
    """A pre-send failure (no SendInput call) must not poison retract.

    The buffer-sync gate fires before any keystroke, so prior credit is
    still safely retractable. last_paste_was_optimistic must remain
    False so the retraction gate does not refuse a legitimate retract.
    """

    def test_buffer_sync_failure_does_not_set_optimistic(
        self, text_perfector, window_manager
    ):
        bm = MagicMock()
        bm.is_valid = False
        bm.synchronize.return_value = False

        clipboard = _make_clipboard_ops()
        clipboard.accumulated_paste_chars = 5  # Prior word's credit.

        strategy = VerifiedUnicodeStrategy(
            bm, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(f"{_STRAT_MOD}.type_string_verified") as mock_send:
            result = strategy.insert("world", context)

        assert result.success is False
        mock_send.assert_not_called()
        # Pre-send failure -- prior credit is still safely retractable.
        assert clipboard.last_paste_was_optimistic is False

    def test_no_focused_control_does_not_set_optimistic(
        self, buffer_manager, text_perfector, window_manager
    ):
        clipboard = _make_clipboard_ops()
        clipboard.accumulated_paste_chars = 5

        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = UIContext(
            focused_control=None,
            is_flutter=False,
            is_terminal=False,
            process_name="notepad.exe",
            class_name="Edit",
            process_id=1234,
        )

        result = strategy.insert("world", context)

        assert result.success is False
        assert clipboard.last_paste_was_optimistic is False
        # Buffer was already valid; pre-send rejection leaves it valid.
        assert buffer_manager.is_valid is True


class TestVerifiedUnicodeStrategyMultiWordWithFailure:
    """The streamed-dictation worst case: success then failure."""

    def test_success_then_partial_send_blocks_retract_credit_carryover(
        self, buffer_manager, text_perfector, window_manager
    ):
        """`hello` succeeds; `world` partial-sends; counter from `hello`
        must not be retractable while uncredited partial text sits after it.
        """
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            return_value=(True, 5, None),
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            assert strategy.insert("hello", context).success is True

        # After success: counter credited, buffer valid, optimistic still False.
        assert clipboard.accumulated_paste_chars == 5
        assert clipboard.last_paste_was_optimistic is False
        assert buffer_manager.is_valid is True

        # Now `world` partial-sends.
        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            return_value=(False, 3, "partial: expected 12 got 6 at chunk offset 0"),
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            assert strategy.insert("world", context).success is False

        # Counter retains prior credit but optimistic flag now blocks retract.
        assert clipboard.accumulated_paste_chars == 5
        assert clipboard.last_paste_was_optimistic is True
        # Shadow buffer invalidated -- next compose will re-sync.
        assert buffer_manager.is_valid is False


class TestVerifiedUnicodeStrategyVerbatim:
    """wh-iti5: VERBATIM mode skips composition and credits raw len."""

    def test_verbatim_skips_perfecter_and_credits_raw_length(
        self, buffer_manager, window_manager
    ):
        """In verbatim mode the text lands exactly as supplied -- no
        prefix space, no capitalization. Counter credit equals
        ``len(text)`` so retract walks the actual delivered length.
        """
        from ui.strategies.base import InsertionMode, InsertionOptions

        clipboard = _make_clipboard_ops()
        text_perfector = MagicMock()
        text_perfector.perfected_string.side_effect = AssertionError(
            "perfecter must not run in verbatim mode"
        )
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            return_value=(True, len("'word'"), None),
        ) as mock_send, patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            result = strategy.insert(
                "'word'",
                context,
                options=InsertionOptions(mode=InsertionMode.VERBATIM),
            )

        assert result.success is True
        # Sent exactly the verbatim string -- no leading space, no caps.
        mock_send.assert_called_once_with("'word'")
        text_perfector.perfected_string.assert_not_called()
        assert clipboard.accumulated_paste_chars == len("'word'")

    def test_verbatim_skips_buffer_sync_gate(
        self, text_perfector, window_manager
    ):
        """Verbatim mode must not refuse on an invalid shadow buffer.

        DICTATION mode requires the buffer to be synchronisable so
        TextPerfector can read preceding context. In VERBATIM mode there
        is no perfecter, so the gate is irrelevant.
        """
        from ui.strategies.base import InsertionMode, InsertionOptions

        clipboard = _make_clipboard_ops()
        bm = ShadowBufferManager()  # un-synchronised: is_valid is False
        bm.synchronize = MagicMock(return_value=False)
        strategy = VerifiedUnicodeStrategy(
            bm, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            return_value=(True, len("verbatim"), None),
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            result = strategy.insert(
                "verbatim",
                context,
                options=InsertionOptions(mode=InsertionMode.VERBATIM),
            )

        assert result.success is True
        # synchronize() must not have been consulted in verbatim mode.
        bm.synchronize.assert_not_called()
        assert clipboard.accumulated_paste_chars == len("verbatim")


class TestVerifiedUnicodeStrategyDispatchInstrumentation:
    """wh-trailing-corruption-instrument: every dispatch increments a
    per-instance ordinal counter and emits a log line carrying the
    ordinal, modifier-key state, length, and first/last codepoint of
    the final string. The first few dispatches per process lifetime
    log at INFO so the wh-startup-trailing-corruption hypothesis
    (corruption clusters near process startup) is observable without
    re-running WheelHouse at DEBUG."""

    def test_dispatch_counter_starts_at_zero(
        self, buffer_manager, text_perfector, window_manager
    ):
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        assert strategy._dispatch_count == 0

    def test_dispatch_counter_increments_on_each_insert(
        self, buffer_manager, text_perfector, window_manager
    ):
        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with patch(
            f"{_STRAT_MOD}.type_string_verified",
            side_effect=lambda text: (True, len(text), None),
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            strategy.insert("hello", context)
            strategy.insert("world", context)
            strategy.insert("again", context)

        assert strategy._dispatch_count == 3

    def test_first_dispatch_logs_at_info_with_ordinal_and_keystate(
        self, buffer_manager, text_perfector, window_manager, caplog
    ):
        import logging

        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with caplog.at_level(logging.INFO, logger="ui.strategies.specific"), patch(
            f"{_STRAT_MOD}.type_string_verified",
            return_value=(True, len("Hello"), None),
        ), patch(
            f"{_STRAT_MOD}.snapshot_modifier_state",
            return_value="shift=- ctrl=- alt=- lwin=- caps=-",
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            strategy.insert("hello", context)

        dispatch_logs = [
            r for r in caplog.records
            if "VerifiedUnicodeStrategy: dispatch" in r.getMessage()
        ]
        assert len(dispatch_logs) == 1
        record = dispatch_logs[0]
        assert record.levelno == logging.INFO
        msg = record.getMessage()
        assert "ord=1" in msg
        assert "shift=-" in msg
        assert "text_len=5" in msg
        assert "process=notepad.exe" in msg

    def test_later_dispatch_logs_at_debug_not_info(
        self, buffer_manager, text_perfector, window_manager, caplog
    ):
        """After the first few dispatches the log drops to DEBUG so a
        long dictation session is not flooded with INFO records.
        Verifies the ordinal threshold is honoured by counting how many
        of the dispatch logs come through at INFO level."""
        import logging

        from ui.strategies import specific as strat_mod

        clipboard = _make_clipboard_ops()
        strategy = VerifiedUnicodeStrategy(
            buffer_manager, text_perfector, clipboard, window_manager
        )
        context = _make_context(hwnd=4242)

        with caplog.at_level(logging.DEBUG, logger="ui.strategies.specific"), patch(
            f"{_STRAT_MOD}.type_string_verified",
            side_effect=lambda text: (True, len(text), None),
        ), patch(
            f"{_STRAT_MOD}.snapshot_modifier_state", return_value="dummy"
        ), patch(
            f"{_STRAT_MOD}.win32gui.GetForegroundWindow", return_value=4242
        ), patch(
            f"{_STRAT_MOD}.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ), patch(
            "ui.hwnd_utils.normalize_hwnd_for_foreground_compare",
            side_effect=_patched_normalize,
        ):
            # Run more dispatches than the INFO threshold.
            total = strat_mod.DISPATCH_INFO_LOG_LIMIT + 2
            for _ in range(total):
                strategy.insert("hi", context)

        dispatch_logs = [
            r for r in caplog.records
            if "VerifiedUnicodeStrategy: dispatch" in r.getMessage()
        ]
        info_count = sum(1 for r in dispatch_logs if r.levelno == logging.INFO)
        debug_count = sum(1 for r in dispatch_logs if r.levelno == logging.DEBUG)
        assert info_count == strat_mod.DISPATCH_INFO_LOG_LIMIT
        assert debug_count == total - strat_mod.DISPATCH_INFO_LOG_LIMIT
