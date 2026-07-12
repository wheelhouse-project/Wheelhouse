"""Tests for ClipboardFallbackStrategy's slow-path text-target preflight.

The router runs the text-target predicate against the focused_control
captured at intelligent_insert_text entry. The slow clipboard fallback
path can take long enough (ShadowBuffer pre-send failure plus clipboard
ops) for the user to click away. The slow-path preflight re-evaluates
the predicate against current focus before any clipboard write so the
fallback does not paste into a non-text target whose focus the user has
since changed (wh-ix1z.9).

References: wh-ix1z.9 (codex-review-loop round 2 finding -- slow
insertion path lacks stale-target preflight), wh-zndq, wh-fc1x.
"""
from unittest.mock import MagicMock, patch

import pytest

from ui.context import UIContext
from ui.strategies.base import InsertionMode, InsertionOptions
from ui.strategies.specific import ClipboardFallbackStrategy
from ui.text_target import TextTargetPredicate, TextTargetVerdict


@pytest.fixture(autouse=True)
def _patch_normalize_hwnd():
    """Patch normalize_hwnd_for_foreground_compare to identity for all tests.

    The slow-path preflight reads top-level HWNDs from mocked controls
    via _hwnd_from_control, which routes through
    normalize_hwnd_for_foreground_compare (real win32gui.GetAncestor).
    Mocked HWND integers are not real Win32 handles so the real call
    raises. Identity-patching matches the pattern used in
    test_clipboard_operations.py and lets each test reason about the
    HWND values it injected. Tests that need a non-identity normalize
    (the wh-ix1z.13 / .14 cases) override with their own `with patch`
    inside the test body.
    """
    with patch(
        "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
        side_effect=lambda h: h if h else None,
    ):
        yield


def _make_context(hwnd: int = 1000) -> UIContext:
    """Build a UIContext whose focused_control resolves to a known HWND.

    The default 1000 is the captured-context HWND. Tests that exercise
    the same-window path provide an identical HWND on the recaptured
    side; tests that exercise the cross-window path provide a different
    one. The autouse normalize patch is identity, so these integers
    flow through _hwnd_from_control unchanged.
    """
    ctrl = MagicMock()
    top = MagicMock()
    top.NativeWindowHandle = hwnd
    ctrl.GetTopLevelControl.return_value = top
    return UIContext(
        focused_control=ctrl,
        is_flutter=False,
        is_terminal=False,
        process_name="brave.exe",
        class_name="textarea medium",
        process_id=1234,
    )


def _make_focus(hwnd: int = 1000):
    """Build a recaptured-focus mock with a known HWND.

    The default 1000 matches the default _make_context HWND so the
    cross-window check accepts; tests that exercise the cross-window
    path pass a different value.
    """
    ctrl = MagicMock()
    top = MagicMock()
    top.NativeWindowHandle = hwnd
    ctrl.GetTopLevelControl.return_value = top
    return ctrl


def _accept_predicate():
    p = MagicMock(spec=TextTargetPredicate)
    p.evaluate.return_value = TextTargetVerdict(
        verdict=True, reason="text_pattern_available",
        supported_patterns=("TextPattern",),
    )
    return p


def _reject_predicate(reason="default_reject"):
    p = MagicMock(spec=TextTargetPredicate)
    p.evaluate.return_value = TextTargetVerdict(
        verdict=False, reason=reason,
        control_type="ListItemControl",
        class_name="UIItem",
        process_name="explorer.exe",
    )
    return p


def _make_strategy(predicate):
    return ClipboardFallbackStrategy(
        buffer_manager=MagicMock(),
        text_perfector=MagicMock(),
        clipboard_ops=MagicMock(),
        window_manager=MagicMock(),
        text_target_predicate=predicate,
    )


# --- TestSlowPathPreflightRejection ---------------------------------------


class TestSlowPathPreflightRejection:
    def test_preflight_rejection_returns_rejected_result(self):
        strategy = _make_strategy(_reject_predicate())
        ctx = _make_context()
        clipboard = strategy.clipboard

        with patch("ui.strategies.specific.auto") as mock_auto:
            mock_auto.GetFocusedControl.return_value = _make_focus()
            result = strategy.insert("hello", ctx)

        assert result.success is True
        assert result.was_rejected is True
        assert result.rejected_reason == "stale_focus_changed_to_non_text"
        assert result.clipboard_dirty is False
        # The clipboard must NOT have been touched by a rejected slow path.
        clipboard.verified_paste.assert_not_called()
        clipboard.clear_selection.assert_not_called()
        clipboard.gather_context.assert_not_called()

    def test_preflight_rejection_in_verbatim_mode_also_returns_rejected(self):
        # The verbatim branch is its own destructive path (verified_paste
        # without context gather). Preflight runs before it too.
        strategy = _make_strategy(_reject_predicate())
        ctx = _make_context()
        clipboard = strategy.clipboard

        with patch("ui.strategies.specific.auto") as mock_auto:
            mock_auto.GetFocusedControl.return_value = _make_focus()
            result = strategy.insert(
                "hello", ctx,
                options=InsertionOptions(mode=InsertionMode.VERBATIM),
            )

        assert result.was_rejected is True
        clipboard.verified_paste.assert_not_called()


# --- TestSlowPathPreflightAcceptance --------------------------------------


class TestSlowPathPreflightAcceptance:
    def test_preflight_acceptance_lets_strategy_proceed(self):
        strategy = _make_strategy(_accept_predicate())
        ctx = _make_context()
        clipboard = strategy.clipboard
        # Make verified_paste succeed so the strategy reaches the end.
        clipboard.verified_paste.return_value = True

        with patch("ui.strategies.specific.auto") as mock_auto, \
             patch("ui.strategies.specific.read_context_via_text_pattern") as mock_read:
            mock_auto.GetFocusedControl.return_value = _make_focus()
            mock_read.return_value = {
                "preceding_chars": "", "has_selection": False,
            }
            result = strategy.insert("hello", ctx)

        # Strategy proceeded to verified_paste -- preflight did not block.
        clipboard.verified_paste.assert_called()
        assert result.success is True
        assert result.was_rejected is False


# --- TestPreflightOptionalForBackCompat -----------------------------------


class TestFlutterShortCircuit:
    """Flutter targets must NOT be subject to the slow-path preflight.

    Flutter widgets commonly do not expose UIA TextPattern; the generic
    predicate would reject them and break Flutter dictation. The
    router-level predicate skips Flutter for the same reason -- the
    slow-path preflight mirrors that short-circuit so FlutterStrategy
    (which inherits from StandardStrategy) keeps its existing behavior.
    """

    def test_flutter_context_skips_preflight(self):
        strategy = _make_strategy(_reject_predicate())
        ctx = UIContext(
            focused_control=MagicMock(),
            is_flutter=True, is_terminal=False,
            process_name="flutter_app.exe",
            class_name="FLUTTERVIEW",
        )
        clipboard = strategy.clipboard
        clipboard.verified_paste.return_value = True

        with patch("ui.strategies.specific.read_context_via_text_pattern") as mock_read:
            mock_read.return_value = {
                "preceding_chars": "", "has_selection": False,
            }
            result = strategy.insert("hello", ctx)

        # The predicate was NOT called because Flutter context bypasses
        # the preflight.
        strategy.text_target_predicate.evaluate.assert_not_called()
        # Flutter dictation reached verified_paste (not blocked).
        clipboard.verified_paste.assert_called()
        assert result.was_rejected is False


class TestPreflightOptionalForBackCompat:
    def test_strategy_without_predicate_skips_preflight(self):
        # Older test fixtures construct ClipboardFallbackStrategy without
        # a predicate. The preflight is silently skipped so existing
        # behaviour is preserved.
        strategy = ClipboardFallbackStrategy(
            buffer_manager=MagicMock(),
            text_perfector=MagicMock(),
            clipboard_ops=MagicMock(),
            window_manager=MagicMock(),
        )
        ctx = _make_context()
        strategy.clipboard.verified_paste.return_value = True

        with patch("ui.strategies.specific.read_context_via_text_pattern") as mock_read:
            mock_read.return_value = {
                "preceding_chars": "", "has_selection": False,
            }
            result = strategy.insert("hello", ctx)

        # The strategy ran the verified_paste path (no preflight to block).
        strategy.clipboard.verified_paste.assert_called()
        assert result.success is True
        assert result.was_rejected is False


# --- TestPreflightCallsPredicateAgainstCurrentFocus -----------------------


class TestCrossWindowFocusChange:
    """wh-ix1z.11 / wh-ix1z.13 / wh-ix1z.14: HWND comparison detects
    cross-window focus change. Both HWNDs are resolved through
    _hwnd_from_control which routes through
    normalize_hwnd_for_foreground_compare (GetAncestor(GA_ROOT)) so
    Chromium / Electron renderer-child HWNDs collapse to their root
    frame. A None resolution on either side fails closed.
    """

    def _hwnd_ctrl(self, hwnd: int):
        # The control's NativeWindowHandle is what _hwnd_from_control
        # reads. The test patches normalize_hwnd_for_foreground_compare
        # to act as identity so the HWND value flows through unchanged.
        ctrl = MagicMock()
        top = MagicMock()
        top.NativeWindowHandle = hwnd
        ctrl.GetTopLevelControl.return_value = top
        return ctrl

    def test_different_hwnd_rejects_even_when_predicate_would_accept(self):
        strategy = _make_strategy(_accept_predicate())
        ctx_ctrl = self._hwnd_ctrl(1000)
        ctx = UIContext(
            focused_control=ctx_ctrl, is_flutter=False, is_terminal=False,
            process_name="brave.exe", class_name="textarea medium",
        )
        new_focus = self._hwnd_ctrl(2000)

        with patch("ui.strategies.specific.auto") as mock_auto, \
             patch("ui.strategies.specific.normalize_hwnd_for_foreground_compare",
                   side_effect=lambda h: h if h else None):
            mock_auto.GetFocusedControl.return_value = new_focus
            result = strategy.insert("hello", ctx)

        assert result.was_rejected is True
        assert result.rejected_reason == "stale_focus_changed_to_non_text"
        strategy.clipboard.verified_paste.assert_not_called()
        strategy.text_target_predicate.evaluate.assert_not_called()

    def test_same_hwnd_proceeds_to_predicate(self):
        strategy = _make_strategy(_accept_predicate())
        ctx_ctrl = self._hwnd_ctrl(1000)
        ctx = UIContext(
            focused_control=ctx_ctrl, is_flutter=False, is_terminal=False,
            process_name="brave.exe", class_name="textarea medium",
        )
        new_focus = self._hwnd_ctrl(1000)
        strategy.clipboard.verified_paste.return_value = True

        with patch("ui.strategies.specific.auto") as mock_auto, \
             patch("ui.strategies.specific.normalize_hwnd_for_foreground_compare",
                   side_effect=lambda h: h if h else None), \
             patch("ui.strategies.specific.read_context_via_text_pattern") as mock_read:
            mock_auto.GetFocusedControl.return_value = new_focus
            mock_read.return_value = {
                "preceding_chars": "", "has_selection": False,
            }
            result = strategy.insert("hello", ctx)

        strategy.text_target_predicate.evaluate.assert_called_once()
        assert result.was_rejected is False

    def test_chromium_renderer_child_normalizes_to_same_root(self):
        # wh-ix1z.13: raw NativeWindowHandle values can differ for two
        # controls under one Chromium window when one is a renderer
        # child. The preflight must use the root-normalized HWND so
        # the same-window case is recognised.
        strategy = _make_strategy(_accept_predicate())
        ctx_ctrl = self._hwnd_ctrl(1100)  # raw renderer-child HWND
        ctx = UIContext(
            focused_control=ctx_ctrl, is_flutter=False, is_terminal=False,
            process_name="brave.exe", class_name="textarea medium",
        )
        new_focus = self._hwnd_ctrl(1200)  # different raw HWND, same root
        strategy.clipboard.verified_paste.return_value = True

        # normalize_hwnd_for_foreground_compare collapses both to the
        # same root frame.
        def normalize(h):
            return 1000 if h in (1100, 1200) else (h if h else None)

        with patch("ui.strategies.specific.auto") as mock_auto, \
             patch("ui.strategies.specific.normalize_hwnd_for_foreground_compare",
                   side_effect=normalize), \
             patch("ui.strategies.specific.read_context_via_text_pattern") as mock_read:
            mock_auto.GetFocusedControl.return_value = new_focus
            mock_read.return_value = {
                "preceding_chars": "", "has_selection": False,
            }
            result = strategy.insert("hello", ctx)

        # Roots match -> predicate ran -> slow path proceeded.
        strategy.text_target_predicate.evaluate.assert_called_once()
        assert result.was_rejected is False


class TestFailClosedOnHWNDReadFailure:
    """wh-ix1z.14 fix: when either side's HWND cannot be resolved, the
    slow path fails closed with a stale-focus rejection. The previous
    fail-open behavior re-opened the wh-ix1z.11 class of bug.
    """

    def _hwnd_ctrl(self, hwnd: int):
        ctrl = MagicMock()
        top = MagicMock()
        top.NativeWindowHandle = hwnd
        ctrl.GetTopLevelControl.return_value = top
        return ctrl

    def _ctx(self, focused_control):
        return UIContext(
            focused_control=focused_control,
            is_flutter=False, is_terminal=False,
            process_name="brave.exe", class_name="textarea medium",
        )

    def test_original_hwnd_unresolvable_rejects(self):
        # Captured control has GetTopLevelControl return None ->
        # _hwnd_from_control returns None -> preflight rejects.
        strategy = _make_strategy(_accept_predicate())
        ctx_ctrl = MagicMock()
        ctx_ctrl.GetTopLevelControl.return_value = None
        ctx = self._ctx(ctx_ctrl)
        new_focus = self._hwnd_ctrl(2000)

        with patch("ui.strategies.specific.auto") as mock_auto, \
             patch("ui.strategies.specific.normalize_hwnd_for_foreground_compare",
                   side_effect=lambda h: h if h else None):
            mock_auto.GetFocusedControl.return_value = new_focus
            result = strategy.insert("hello", ctx)

        assert result.was_rejected is True
        assert result.rejected_reason == "stale_focus_changed_to_non_text"
        strategy.text_target_predicate.evaluate.assert_not_called()
        strategy.clipboard.verified_paste.assert_not_called()

    def test_current_hwnd_unresolvable_rejects(self):
        # Recaptured control has zero NativeWindowHandle ->
        # _hwnd_from_control returns None (the `if not hwnd` guard) ->
        # preflight rejects.
        strategy = _make_strategy(_accept_predicate())
        ctx_ctrl = self._hwnd_ctrl(1000)
        ctx = self._ctx(ctx_ctrl)
        new_focus = self._hwnd_ctrl(0)  # zero HWND on the recaptured side

        with patch("ui.strategies.specific.auto") as mock_auto, \
             patch("ui.strategies.specific.normalize_hwnd_for_foreground_compare",
                   side_effect=lambda h: h if h else None):
            mock_auto.GetFocusedControl.return_value = new_focus
            result = strategy.insert("hello", ctx)

        assert result.was_rejected is True
        strategy.text_target_predicate.evaluate.assert_not_called()
        strategy.clipboard.verified_paste.assert_not_called()

    def test_normalize_returns_none_rejects(self):
        # Even with a non-zero NativeWindowHandle, if
        # normalize_hwnd_for_foreground_compare returns None for either
        # side, the preflight must still reject.
        strategy = _make_strategy(_accept_predicate())
        ctx_ctrl = self._hwnd_ctrl(1000)
        ctx = self._ctx(ctx_ctrl)
        new_focus = self._hwnd_ctrl(2000)

        with patch("ui.strategies.specific.auto") as mock_auto, \
             patch("ui.strategies.specific.normalize_hwnd_for_foreground_compare",
                   side_effect=lambda h: None if h == 2000 else h):
            mock_auto.GetFocusedControl.return_value = new_focus
            result = strategy.insert("hello", ctx)

        assert result.was_rejected is True
        strategy.text_target_predicate.evaluate.assert_not_called()


class TestPreflightHWNDCarriedForwardToPaste:
    """wh-ix1z.17 fix: the validated HWND from the preflight is the
    HWND passed to verified_paste, not a fresh _hwnd_from_control
    lookup that could observe a stale captured control between the
    preflight and the paste call.
    """

    def _hwnd_ctrl(self, hwnd: int):
        ctrl = MagicMock()
        top = MagicMock()
        top.NativeWindowHandle = hwnd
        ctrl.GetTopLevelControl.return_value = top
        return ctrl

    def test_verified_paste_receives_validated_hwnd_not_late_lookup(self):
        # The preflight resolves the captured HWND to 1000. Between
        # the preflight and verified_paste, the captured control's
        # GetTopLevelControl is patched to start returning None
        # (simulating staleness). verified_paste must still see
        # target_hwnd=1000 because the strategy carries the validated
        # value forward.
        strategy = _make_strategy(_accept_predicate())
        ctx_ctrl = self._hwnd_ctrl(1000)
        ctx = UIContext(
            focused_control=ctx_ctrl, is_flutter=False, is_terminal=False,
            process_name="brave.exe", class_name="textarea medium",
        )
        new_focus = self._hwnd_ctrl(1000)
        strategy.clipboard.verified_paste.return_value = True

        # Track _hwnd_from_control calls so we can flip the captured
        # control's behavior after the preflight reads it. Two preflight
        # reads happen first (original then current); after that, any
        # new lookup against the captured control returns None.
        original_calls = []

        def captured_top_level():
            original_calls.append(True)
            if len(original_calls) <= 1:
                top = MagicMock()
                top.NativeWindowHandle = 1000
                return top
            return None  # late lookup observes staleness

        ctx_ctrl.GetTopLevelControl.side_effect = captured_top_level

        with patch("ui.strategies.specific.auto") as mock_auto, \
             patch("ui.strategies.specific.read_context_via_text_pattern") as mock_read:
            mock_auto.GetFocusedControl.return_value = new_focus
            mock_read.return_value = {
                "preceding_chars": "", "has_selection": False,
            }
            result = strategy.insert("hello", ctx)

        assert result.success is True
        assert result.was_rejected is False
        # verified_paste was called; the target_hwnd argument is the
        # validated value (1000), not None from a late stale lookup.
        call = strategy.clipboard.verified_paste.call_args
        target_hwnd = call.kwargs.get("target_hwnd")
        assert target_hwnd == 1000, (
            f"verified_paste received target_hwnd={target_hwnd!r}; "
            f"expected 1000 (the preflight's validated HWND)"
        )

    def test_verbatim_branch_also_uses_validated_hwnd(self):
        strategy = _make_strategy(_accept_predicate())
        ctx_ctrl = self._hwnd_ctrl(1000)
        ctx = UIContext(
            focused_control=ctx_ctrl, is_flutter=False, is_terminal=False,
            process_name="brave.exe", class_name="textarea medium",
        )
        new_focus = self._hwnd_ctrl(1000)
        strategy.clipboard.verified_paste.return_value = True

        # After preflight reads the captured top-level twice (original
        # and current paths in the preflight... actually only one read
        # for the captured side), simulate staleness on subsequent
        # lookups.
        captured_seen = [0]

        def captured_top_level():
            captured_seen[0] += 1
            if captured_seen[0] == 1:
                top = MagicMock()
                top.NativeWindowHandle = 1000
                return top
            return None

        ctx_ctrl.GetTopLevelControl.side_effect = captured_top_level

        with patch("ui.strategies.specific.auto") as mock_auto:
            mock_auto.GetFocusedControl.return_value = new_focus
            result = strategy.insert(
                "hello", ctx,
                options=InsertionOptions(mode=InsertionMode.VERBATIM),
            )

        assert result.success is True
        call = strategy.clipboard.verified_paste.call_args
        assert call.kwargs.get("target_hwnd") == 1000


class TestPreflightDoesNotPropagateContextClassName:
    """wh-ix1z.15 fix: the preflight passes class_name="" to the
    predicate so the rejection telemetry does not record the captured
    context's class on top of the freshly recaptured control.
    """

    def _hwnd_ctrl(self, hwnd: int):
        ctrl = MagicMock()
        top = MagicMock()
        top.NativeWindowHandle = hwnd
        ctrl.GetTopLevelControl.return_value = top
        return ctrl

    def test_preflight_evaluate_called_with_empty_class_name(self):
        strategy = _make_strategy(_accept_predicate())
        ctx_ctrl = self._hwnd_ctrl(1000)
        ctx = UIContext(
            focused_control=ctx_ctrl, is_flutter=False, is_terminal=False,
            process_name="brave.exe",
            class_name="textarea medium",  # captured-context class
        )
        new_focus = self._hwnd_ctrl(1000)
        strategy.clipboard.verified_paste.return_value = True

        with patch("ui.strategies.specific.auto") as mock_auto, \
             patch("ui.strategies.specific.normalize_hwnd_for_foreground_compare",
                   side_effect=lambda h: h if h else None), \
             patch("ui.strategies.specific.read_context_via_text_pattern") as mock_read:
            mock_auto.GetFocusedControl.return_value = new_focus
            mock_read.return_value = {
                "preceding_chars": "", "has_selection": False,
            }
            strategy.insert("hello", ctx)

        kwargs = strategy.text_target_predicate.evaluate.call_args.kwargs
        assert kwargs["class_name"] == ""
        # process_name still flows through because it is correlation,
        # not class attribution.
        assert kwargs["process_name"] == "brave.exe"


class TestPreflightUIAInitializerScope:
    """wh-ix1z.12 fix: the predicate's UIA reads must run inside the
    UIAutomationInitializerInThread block. Property reads and
    GetPattern calls outside the initializer can produce
    COM-uninitialized errors on the slow-path thread.
    """

    def test_predicate_evaluate_called_inside_initializer(self):
        strategy = _make_strategy(_accept_predicate())
        ctx = _make_context()
        strategy.clipboard.verified_paste.return_value = True

        # Track ordering: initializer.__enter__, GetFocusedControl,
        # predicate.evaluate, initializer.__exit__.
        events: list[str] = []
        initializer_cm = MagicMock()
        initializer_cm.__enter__.side_effect = lambda: events.append("enter")
        initializer_cm.__exit__.side_effect = lambda *a: events.append("exit")

        def get_focused_control():
            events.append("get_focused")
            # Return a focus mock with a HWND that matches the context
            # so the new HWND comparison does not short-circuit before
            # the predicate runs (the test's purpose is to verify the
            # predicate is called inside the initializer).
            return _make_focus()

        # Patch evaluate to record when it ran relative to enter / exit.
        original_evaluate = strategy.text_target_predicate.evaluate.side_effect
        def evaluate(*a, **kw):
            events.append("evaluate")
            return TextTargetVerdict(
                verdict=True, reason="text_pattern_available",
                supported_patterns=("TextPattern",),
            )
        strategy.text_target_predicate.evaluate.side_effect = evaluate
        strategy.text_target_predicate.evaluate.return_value = None

        with patch("ui.strategies.specific.auto") as mock_auto, \
             patch("ui.strategies.specific.read_context_via_text_pattern") as mock_read:
            mock_auto.UIAutomationInitializerInThread.return_value = initializer_cm
            mock_auto.GetFocusedControl.side_effect = get_focused_control
            mock_read.return_value = {
                "preceding_chars": "", "has_selection": False,
            }
            strategy.insert("hello", ctx)

        # Predicate.evaluate must come BEFORE the exit event -- i.e.,
        # while COM is still initialized.
        assert "enter" in events
        assert "get_focused" in events
        assert "evaluate" in events
        assert "exit" in events
        # wh-ix1z.16: assert the FULL ordering. Asserting only
        # evaluate_idx < exit_idx would miss a regression that moved
        # evaluate to BEFORE the with-block (events would be
        # ["evaluate", "enter", "get_focused", "exit"]) -- evaluate
        # would still precede exit but COM would not be initialized
        # when evaluate ran.
        enter_idx = events.index("enter")
        get_focused_idx = events.index("get_focused")
        evaluate_idx = events.index("evaluate")
        exit_idx = events.index("exit")
        assert enter_idx < get_focused_idx < evaluate_idx < exit_idx, (
            f"predicate.evaluate did not run between initializer __enter__ "
            f"and __exit__ (events={events})"
        )

    def test_uia_failure_inside_initializer_returns_rejection(self):
        # If GetFocusedControl raises, the slow path must fail closed
        # with the same rejection result rather than silently proceed.
        strategy = _make_strategy(_accept_predicate())
        ctx = _make_context()

        with patch("ui.strategies.specific.auto") as mock_auto:
            mock_auto.GetFocusedControl.side_effect = OSError("COM not initialized")
            result = strategy.insert("hello", ctx)

        assert result.was_rejected is True
        assert result.rejected_reason == "stale_focus_changed_to_non_text"
        strategy.clipboard.verified_paste.assert_not_called()


class TestPreflightCallsPredicateAgainstCurrentFocus:
    def test_preflight_recaptures_focus_via_get_focused_control(self):
        # The point of the slow-path preflight is to detect focus change
        # AFTER intelligent_insert_text captured context. Confirm the
        # strategy calls GetFocusedControl, not just reuses
        # context.focused_control.
        strategy = _make_strategy(_accept_predicate())
        ctx = _make_context()
        strategy.clipboard.verified_paste.return_value = True

        with patch("ui.strategies.specific.auto") as mock_auto, \
             patch("ui.strategies.specific.read_context_via_text_pattern") as mock_read:
            new_focus = _make_focus()  # same HWND as the captured context
            new_focus.name = "post-stale-focus"  # diagnostic label
            mock_auto.GetFocusedControl.return_value = new_focus
            mock_read.return_value = {
                "preceding_chars": "", "has_selection": False,
            }
            strategy.insert("hello", ctx)

        # The predicate received the freshly captured focus, NOT
        # ctx.focused_control.
        passed_focus = strategy.text_target_predicate.evaluate.call_args.args[0]
        assert passed_focus is new_focus
        assert passed_focus is not ctx.focused_control

    def test_preflight_forwards_process_name_to_predicate(self):
        # process_name is correlation-only telemetry, so the preflight
        # forwards context.process_name to the predicate. class_name is
        # NOT forwarded (wh-ix1z.15 fix); see
        # TestPreflightDoesNotPropagateContextClassName.
        strategy = _make_strategy(_accept_predicate())
        ctx = _make_context()
        strategy.clipboard.verified_paste.return_value = True

        with patch("ui.strategies.specific.auto") as mock_auto, \
             patch("ui.strategies.specific.read_context_via_text_pattern") as mock_read:
            mock_auto.GetFocusedControl.return_value = _make_focus()
            mock_read.return_value = {
                "preceding_chars": "", "has_selection": False,
            }
            strategy.insert("hello", ctx)

        kwargs = strategy.text_target_predicate.evaluate.call_args.kwargs
        assert kwargs["process_name"] == "brave.exe"
