"""Tests for ui.router.InsertionRouter.

History:
- wh-606yk introduced VerifiedUnicodeStrategy and the length-based default
  branch.
- wh-zndq / wh-fc1x introduced the shared text-target predicate and the
  RejectedInsertionStrategy. Existing tests that asserted SimplePaste
  fallback for no-focused-control / not-focusable / stale-COM now route
  to RejectedInsertionStrategy when the predicate is wired -- the legacy
  SimplePaste fallback is preserved only when the router is constructed
  without a predicate (the legacy_focusable_check branch).
- wh-1g6er removed the terminal-editor branch entirely; the focus-redirect
  path opens an empty editor and drained words flow through Standard /
  VerifiedUnicode against the editor's QPlainTextEdit (UIA TextPattern).
"""
import _ctypes
from unittest.mock import MagicMock, PropertyMock

import pytest

from ui.context import UIContext
from ui.router import InsertionRouter
from ui.text_target import TextTargetPredicate, TextTargetVerdict


# --- Helpers ---------------------------------------------------------------


@pytest.fixture
def strategies():
    return {
        "standard": MagicMock(name="StandardStrategy"),
        "flutter": MagicMock(name="FlutterStrategy"),
        "simple_paste": MagicMock(name="SimplePasteStrategy"),
        "rejected": MagicMock(name="RejectedInsertionStrategy"),
        "verified_unicode": MagicMock(name="VerifiedUnicodeStrategy"),
    }


def _stub_predicate(verdicts):
    """Build a TextTargetPredicate stand-in.

    ``verdicts`` is either a single TextTargetVerdict (returned for every
    call) or a callable that takes (focused_control, *, class_name,
    process_name) and returns a verdict.
    """
    predicate = MagicMock(spec=TextTargetPredicate)
    if callable(verdicts):
        predicate.evaluate.side_effect = lambda *a, **kw: verdicts(*a, **kw)
    else:
        predicate.evaluate.return_value = verdicts
    return predicate


def _accept_predicate():
    return _stub_predicate(TextTargetVerdict(
        verdict=True, reason="text_pattern_available",
        supported_patterns=("TextPattern",),
        control_type="EditControl", class_name="Edit",
    ))


def _reject_predicate(reason="default_reject"):
    return _stub_predicate(TextTargetVerdict(
        verdict=False, reason=reason,
        control_type="ListItemControl", class_name="UIItem",
        process_name="explorer.exe",
    ))


@pytest.fixture
def router(strategies):
    """Production-shape router: predicate accepts every text target."""
    return InsertionRouter(
        standard_strategy=strategies["standard"],
        flutter_strategy=strategies["flutter"],
        simple_paste_strategy=strategies["simple_paste"],
        rejected_strategy=strategies["rejected"],
        text_target_predicate=_accept_predicate(),
        verified_unicode_strategy=strategies["verified_unicode"],
        verified_unicode_max_chars=50,
    )


@pytest.fixture
def router_legacy(strategies):
    """Legacy-shape router: no predicate wired.

    Mimics older callers and tests that have not been updated to pass a
    text-target predicate. The router falls back to the focusable-only
    check for those callers.
    """
    return InsertionRouter(
        standard_strategy=strategies["standard"],
        flutter_strategy=strategies["flutter"],
        simple_paste_strategy=strategies["simple_paste"],
        verified_unicode_strategy=strategies["verified_unicode"],
        verified_unicode_max_chars=50,
    )


def _focusable_ctx(*, is_flutter=False, is_terminal=False, process="brave.exe",
                   class_name="Chrome_WidgetWin_1"):
    ctrl = MagicMock()
    ctrl.IsKeyboardFocusable = True
    return UIContext(focused_control=ctrl, is_flutter=is_flutter,
                     is_terminal=is_terminal, process_name=process,
                     class_name=class_name)


# --- TestGetStrategy (predicate-wired router) ------------------------------


class TestGetStrategy:
    def test_flutter_returns_flutter_strategy(self, router, strategies):
        ctrl = MagicMock()
        ctrl.IsKeyboardFocusable = True
        ctx = UIContext(focused_control=ctrl, is_flutter=True, is_terminal=False,
                        process_name="flutter_app.exe", class_name="FLUTTERVIEW")
        assert router.get_strategy(ctx) is strategies["flutter"]

    def test_default_with_no_insertion_string_returns_standard(self, router, strategies):
        """Without a text length, the default branch falls back to Standard."""
        ctx = _focusable_ctx()
        assert router.get_strategy(ctx) is strategies["standard"]


# --- TestPredicateRouting (wh-zndq, wh-fc1x) -------------------------------


class TestPredicateRouting:
    """The shared text-target predicate decides accept vs. reject.

    Reject routes to RejectedInsertionStrategy regardless of length;
    accept routes to VerifiedUnicode / Standard by the existing length
    check.
    """

    def test_predicate_rejection_routes_to_rejected(self, strategies):
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
                simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=_reject_predicate("denylist_control_type"),
            verified_unicode_strategy=strategies["verified_unicode"],
            verified_unicode_max_chars=50,
        )
        ctx = _focusable_ctx()
        assert router.get_strategy(ctx, "hello") is strategies["rejected"]

    def test_predicate_rejection_short_circuits_length_branch(self, strategies):
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
                simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=_reject_predicate(),
            verified_unicode_strategy=strategies["verified_unicode"],
            verified_unicode_max_chars=50,
        )
        ctx = _focusable_ctx()
        # Long text would normally route to StandardStrategy; rejected
        # short-circuits before length check.
        assert router.get_strategy(ctx, "x" * 200) is strategies["rejected"]

    def test_router_passes_verdict_to_rejected_strategy(self, strategies):
        """wh-7318z: the router calls set_pending_verdict on the
        rejected strategy with the verdict, so the strategy can emit a
        text_target_rejected event during insert."""

        verdict = TextTargetVerdict(
            verdict=False,
            reason="default_reject",
            control_type="Pane",
            class_name="zed::Workspace",
            process_name="zed.exe",
            supported_patterns=("Invoke",),
        )
        predicate = _stub_predicate(verdict)
        # MagicMock auto-generates set_pending_verdict so we can assert
        # the call without sublcassing.
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
                simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=predicate,
            verified_unicode_strategy=strategies["verified_unicode"],
            verified_unicode_max_chars=50,
        )
        result = router.get_strategy(_focusable_ctx(), "hello")
        assert result is strategies["rejected"]
        strategies["rejected"].set_pending_verdict.assert_called_once_with(verdict)

    def test_unknown_soft_reject_routes_to_rejected_with_verdict(self, strategies):
        """wh-soft-allow-verdict-tier: an unknown soft-reject (reason
        ``default_reject_paste_capable_class``) routes to
        RejectedInsertionStrategy and the router passes the verdict via
        set_pending_verdict so the strategy can emit text_target_rejected.
        ClipboardOnlyStrategy is reserved for known-tuple accepts. Even
        when clipboard_only_strategy is wired, an unknown soft-reject
        must NOT silently paste -- it must front the override toast."""

        verdict = TextTargetVerdict(
            verdict=False,
            reason="default_reject_paste_capable_class",
            control_type="Pane",
            class_name="zed::Workspace",
            process_name="zed.exe",
            supported_patterns=("ValuePattern",),
        )
        clipboard_only = MagicMock(name="ClipboardOnlyStrategy")
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
                simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=_stub_predicate(verdict),
            verified_unicode_strategy=strategies["verified_unicode"],
            clipboard_only_strategy=clipboard_only,
            verified_unicode_max_chars=50,
        )
        result = router.get_strategy(_focusable_ctx(), "hello")
        assert result is strategies["rejected"]
        strategies["rejected"].set_pending_verdict.assert_called_once_with(verdict)

    def test_known_soft_allow_tuple_routes_to_clipboard_only(self, strategies):
        """wh-soft-allow-verdict-tier: a predicate accept with reason
        ``accept_soft_allow_tuple`` routes to ClipboardOnlyStrategy for
        silent paste. The length-based default branch is short-circuited
        so a 200-character utterance still goes through ClipboardOnly."""

        verdict = TextTargetVerdict(
            verdict=True,
            reason="accept_soft_allow_tuple",
            control_type="WindowControl",
            class_name="Zed::Window",
            process_name="zed.exe",
        )
        clipboard_only = MagicMock(name="ClipboardOnlyStrategy")
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
                simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=_stub_predicate(verdict),
            verified_unicode_strategy=strategies["verified_unicode"],
            clipboard_only_strategy=clipboard_only,
            verified_unicode_max_chars=50,
        )
        assert router.get_strategy(_focusable_ctx(), "hello") is clipboard_only
        assert router.get_strategy(_focusable_ctx(), "x" * 200) is clipboard_only
        # The accept path must NOT call set_pending_verdict on the
        # rejected strategy -- the soft-allow accept is not a rejection.
        strategies["rejected"].set_pending_verdict.assert_not_called()

    def test_soft_allow_accept_without_clipboard_only_falls_through_to_default(
        self, strategies,
    ):
        """Legacy fixtures that wire the predicate but not ClipboardOnly
        must keep working. The accept_soft_allow_tuple verdict falls
        through to the default length-based branch when
        clipboard_only_strategy is None, so VerifiedUnicodeStrategy /
        StandardStrategy handle the insertion as for any other accept."""

        verdict = TextTargetVerdict(
            verdict=True,
            reason="accept_soft_allow_tuple",
            control_type="WindowControl",
            class_name="Zed::Window",
            process_name="zed.exe",
        )
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
                simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=_stub_predicate(verdict),
            verified_unicode_strategy=strategies["verified_unicode"],
            verified_unicode_max_chars=50,
            # clipboard_only_strategy intentionally omitted.
        )
        # Short text falls through to VerifiedUnicode.
        assert (
            router.get_strategy(_focusable_ctx(), "hello")
            is strategies["verified_unicode"]
        )
        # Long text falls through to Standard.
        assert (
            router.get_strategy(_focusable_ctx(), "x" * 200)
            is strategies["standard"]
        )

    def test_router_tolerates_rejected_without_set_pending_verdict(self, strategies):
        """wh-7318z: a legacy rejected strategy that lacks
        set_pending_verdict (older test fixtures or a future replacement
        strategy) must not break routing."""

        legacy_rejected = MagicMock(spec=[])  # no attributes at all
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
                simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=legacy_rejected,
            text_target_predicate=_reject_predicate(),
            verified_unicode_strategy=strategies["verified_unicode"],
            verified_unicode_max_chars=50,
        )
        result = router.get_strategy(_focusable_ctx(), "hello")
        assert result is legacy_rejected

    def test_predicate_acceptance_routes_to_unicode_for_short_text(self, router, strategies):
        ctx = _focusable_ctx()
        assert router.get_strategy(ctx, "hello") is strategies["verified_unicode"]

    def test_predicate_acceptance_routes_to_standard_for_long_text(self, router, strategies):
        ctx = _focusable_ctx()
        assert router.get_strategy(ctx, "x" * 200) is strategies["standard"]

    def test_broken_input_pipeline_class_forces_standard_for_short_text(
        self, router, strategies,
    ):
        """wh-notepad-clipboard-workaround: Win11 modern Notepad's
        RichEditD2DPT control mishandles KEYEVENTF_UNICODE SendInput.
        For these controls the router must skip VerifiedUnicodeStrategy
        and force StandardStrategy (clipboard paste) regardless of
        insertion length. The predicate still accepts as normal -- the
        workaround only changes which accept-branch strategy the router
        picks.
        """
        ctx = _focusable_ctx(
            process="notepad.exe", class_name="RichEditD2DPT",
        )
        # Short text would normally go to VerifiedUnicodeStrategy.
        assert router.get_strategy(ctx, "hello") is strategies["standard"]
        # Long text already went to Standard; behavior unchanged.
        assert router.get_strategy(ctx, "x" * 200) is strategies["standard"]

    def test_broken_input_pipeline_class_does_not_affect_other_apps(
        self, router, strategies,
    ):
        """The workaround applies only to known-broken control classes.
        Apps that work fine with SendInput keep the existing length-based
        routing.
        """
        ctx = _focusable_ctx(
            process="notepad++.exe", class_name="Scintilla",
        )
        assert router.get_strategy(ctx, "hello") is strategies["verified_unicode"]
        assert router.get_strategy(ctx, "x" * 200) is strategies["standard"]

    def test_broken_input_pipeline_class_loses_to_soft_allow(self, strategies):
        """If a target with a broken-input-pipeline class name is ALSO
        in the user's approved-control list, the soft-allow accept
        tier takes priority and keeps the silent ClipboardOnly behavior.
        Both branches use clipboard paste, but soft-allow routes to a
        different strategy that suppresses the post-paste rejection
        notice. The workaround branch must not steal that.
        """
        verdict = TextTargetVerdict(
            verdict=True,
            reason="accept_soft_allow_tuple",
            control_type="DocumentControl",
            class_name="RichEditD2DPT",
            process_name="notepad.exe",
        )
        clipboard_only = MagicMock(name="ClipboardOnlyStrategy")
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
            simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=_stub_predicate(verdict),
            verified_unicode_strategy=strategies["verified_unicode"],
            clipboard_only_strategy=clipboard_only,
            verified_unicode_max_chars=50,
        )
        ctx = _focusable_ctx(
            process="notepad.exe", class_name="RichEditD2DPT",
        )
        assert router.get_strategy(ctx, "hello") is clipboard_only

    def test_flutter_short_circuits_predicate(self, strategies):
        """Flutter context never invokes the predicate."""
        predicate = MagicMock(spec=TextTargetPredicate)
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
            simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=predicate,
        )
        ctrl = MagicMock()
        ctrl.IsKeyboardFocusable = True
        ctx = UIContext(focused_control=ctrl, is_flutter=True, is_terminal=False,
                        process_name="flutter_app.exe", class_name="FLUTTERVIEW")
        assert router.get_strategy(ctx) is strategies["flutter"]
        predicate.evaluate.assert_not_called()

    def test_predicate_passes_class_and_process_from_context(self, strategies):
        captured = {}
        def evaluate(_focused_control, *, class_name, process_name):
            captured["class_name"] = class_name
            captured["process_name"] = process_name
            return TextTargetVerdict(
                verdict=True, reason="text_pattern_available",
                supported_patterns=("TextPattern",),
            )
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
                simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=_stub_predicate(evaluate),
        )
        ctx = _focusable_ctx(class_name="textarea medium", process="brave.exe")
        router.get_strategy(ctx, "hi")
        assert captured == {
            "class_name": "textarea medium",
            "process_name": "brave.exe",
        }

    def test_rejection_logs_telemetry_at_debug(self, caplog, strategies):
        """Rejection emits a DEBUG-level log line with telemetry fields.

        wh-ix1z.7: routine rejections must NOT log at INFO. Background
        speech and repeated dictation while focus is on a non-text
        control would flood the log with one INFO line per word
        otherwise.
        """
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
                simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=_stub_predicate(TextTargetVerdict(
                verdict=False, reason="denylist_class_name",
                control_type="MenuItemControl", class_name="MenuFlyoutSubItem",
                process_name="notepad.exe",
            )),
        )
        ctx = _focusable_ctx()
        with caplog.at_level("DEBUG", logger="ui.router"):
            router.get_strategy(ctx, "hi")
        debug_records = [
            r for r in caplog.records
            if r.name == "ui.router" and r.levelname == "DEBUG"
        ]
        assert any(
            "rejected text target" in r.getMessage()
            and "denylist_class_name" in r.getMessage()
            and "MenuFlyoutSubItem" in r.getMessage()
            and "notepad.exe" in r.getMessage()
            for r in debug_records
        )

    def test_rejection_does_not_log_at_info(self, caplog, strategies):
        """Routine rejections must NOT emit at INFO level (wh-ix1z.7)."""
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
                simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=_reject_predicate(),
        )
        ctx = _focusable_ctx()
        with caplog.at_level("INFO", logger="ui.router"):
            router.get_strategy(ctx, "hi")
        info_records = [
            r for r in caplog.records
            if r.name == "ui.router" and r.levelname == "INFO"
            and "rejected text target" in r.getMessage()
        ]
        assert info_records == []


# --- TestLegacyFocusableCheck (router built without a predicate) -----------


class TestLegacyFocusableCheck:
    """Older callers that construct the router without a TextTargetPredicate
    keep the legacy focusable-only behaviour. Production code always wires
    the predicate; these tests guard the back-compat path used by older
    fixtures still in the repo.
    """

    def test_no_focused_control_returns_simple_paste(self, router_legacy, strategies):
        ctx = UIContext(focused_control=None, is_flutter=False, is_terminal=False,
                        process_name="test.exe", class_name="")
        assert router_legacy.get_strategy(ctx) is strategies["simple_paste"]

    def test_not_focusable_returns_simple_paste(self, router_legacy, strategies):
        ctrl = MagicMock()
        ctrl.IsKeyboardFocusable = False
        ctx = UIContext(focused_control=ctrl, is_flutter=False, is_terminal=False,
                        process_name="test.exe", class_name="")
        assert router_legacy.get_strategy(ctx) is strategies["simple_paste"]

    def test_stale_com_element_returns_simple_paste(self, router_legacy, strategies):
        ctrl = MagicMock()
        type(ctrl).IsKeyboardFocusable = PropertyMock(
            side_effect=_ctypes.COMError(-2147220991,
                "An event was unable to invoke any of the subscribers",
                (None, None, None, 0, None))
        )
        ctx = UIContext(focused_control=ctrl, is_flutter=False, is_terminal=False,
                        process_name="brave.exe", class_name="Chrome_WidgetWin_1")
        assert router_legacy.get_strategy(ctx) is strategies["simple_paste"]

    def test_generic_exception_on_focusable_check_returns_simple_paste(self, router_legacy, strategies):
        ctrl = MagicMock()
        type(ctrl).IsKeyboardFocusable = PropertyMock(side_effect=OSError("access denied"))
        ctx = UIContext(focused_control=ctrl, is_flutter=False, is_terminal=False,
                        process_name="test.exe", class_name="")
        assert router_legacy.get_strategy(ctx) is strategies["simple_paste"]


# --- TestVerifiedUnicodeRouting --------------------------------------------


class TestVerifiedUnicodeRouting:
    """wh-606yk: short text in normal apps prefers VerifiedUnicodeStrategy.

    Long text drops to StandardStrategy. Terminal and Flutter routing
    are unchanged regardless of length so the per-app strategies always
    win on those targets.
    """

    def test_short_text_in_normal_app_routes_to_unicode(self, router, strategies):
        ctx = _focusable_ctx()
        assert router.get_strategy(ctx, "hello") is strategies["verified_unicode"]

    def test_long_text_in_normal_app_routes_to_standard(self, router, strategies):
        ctx = _focusable_ctx()
        long_text = "x" * 51
        assert router.get_strategy(ctx, long_text) is strategies["standard"]

    def test_threshold_boundary_inclusive_at_max(self, router, strategies):
        ctx = _focusable_ctx()
        boundary_text = "x" * 50
        assert router.get_strategy(ctx, boundary_text) is strategies["verified_unicode"]

    def test_one_over_threshold_routes_to_standard(self, router, strategies):
        ctx = _focusable_ctx()
        over_text = "x" * 51
        assert router.get_strategy(ctx, over_text) is strategies["standard"]

    def test_flutter_unaffected_by_short_text(self, router, strategies):
        ctrl = MagicMock()
        ctrl.IsKeyboardFocusable = True
        ctx = UIContext(focused_control=ctrl, is_flutter=True, is_terminal=False,
                        process_name="flutter_app.exe", class_name="FLUTTERVIEW")
        assert router.get_strategy(ctx, "hi") is strategies["flutter"]

    def test_legacy_router_without_predicate_no_focus_returns_simple_paste(
        self, router_legacy, strategies
    ):
        """No-focused-control on a legacy (no-predicate) router still routes to
        SimplePaste regardless of length."""
        ctx = UIContext(focused_control=None, is_flutter=False, is_terminal=False,
                        process_name="test.exe", class_name="")
        assert router_legacy.get_strategy(ctx, "hi") is strategies["simple_paste"]

    def test_router_without_unicode_falls_back_to_standard(self, strategies):
        """Predicate-wired router built without verified_unicode_strategy
        keeps StandardStrategy for short text."""
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
                simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=_accept_predicate(),
        )
        ctx = _focusable_ctx()
        assert router.get_strategy(ctx, "hi") is strategies["standard"]

    def test_custom_threshold_respected(self, strategies):
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
                simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=_accept_predicate(),
            verified_unicode_strategy=strategies["verified_unicode"],
            verified_unicode_max_chars=10,
        )
        ctx = _focusable_ctx()
        assert router.get_strategy(ctx, "x" * 10) is strategies["verified_unicode"]
        assert router.get_strategy(ctx, "x" * 11) is strategies["standard"]


# --- TestUIContextProcessId ------------------------------------------------


class TestUIContextProcessId:
    def test_uicontext_has_process_id_field(self):
        ctx = UIContext(
            focused_control=None, is_flutter=False, is_terminal=False,
            process_name="test.exe", class_name="", process_id=1234,
        )
        assert ctx.process_id == 1234

    def test_uicontext_process_id_defaults_to_zero(self):
        ctx = UIContext(
            focused_control=None, is_flutter=False, is_terminal=False,
            process_name="test.exe", class_name="",
        )
        assert ctx.process_id == 0


# --- TestElevationRouting (wh-elevated-target-notice) ----------------------


class TestElevationRouting:
    """The elevation check runs at the top of the predicate step,
    BEFORE evaluate and before the soft-allow silent-paste tier: an
    approved control relaunched as administrator must not take the
    silent path, and the check must not depend on UI Automation
    visibility (which is unreliable for elevated windows).

    Contract: checker returns "elevated" -> RejectedInsertionStrategy
    with a synthesized verdict (reason "elevated_process_window");
    "not_elevated"/"unknown"/raise -> the existing pipeline runs
    unchanged (fail open).
    """

    def _router(self, strategies, *, checker, predicate=None,
                clipboard_only=None):
        return InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
            simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=predicate or _accept_predicate(),
            verified_unicode_strategy=strategies["verified_unicode"],
            verified_unicode_max_chars=50,
            clipboard_only_strategy=clipboard_only,
            elevation_checker=checker,
        )

    def test_elevated_routes_to_rejected(self, strategies):
        predicate = _accept_predicate()
        router = self._router(
            strategies, checker=lambda ctrl: "elevated",
            predicate=predicate,
        )
        ctx = _focusable_ctx(process="regedit.exe",
                             class_name="RegEdit_RegEdit")
        assert router.get_strategy(ctx, "hello") is strategies["rejected"]
        # The predicate is never consulted: UIA visibility into the
        # elevated window is unreliable, and its answer could not
        # change the routing anyway.
        predicate.evaluate.assert_not_called()

    def test_elevated_verdict_carries_reason_and_context_identity(
        self, strategies,
    ):
        router = self._router(strategies, checker=lambda ctrl: "elevated")
        ctx = _focusable_ctx(process="regedit.exe",
                             class_name="RegEdit_RegEdit")
        router.get_strategy(ctx, "hello")
        strategies["rejected"].set_pending_verdict.assert_called_once()
        verdict = strategies["rejected"].set_pending_verdict.call_args[0][0]
        assert verdict.verdict is False
        assert verdict.reason == "elevated_process_window"
        assert verdict.process_name == "regedit.exe"
        assert verdict.class_name == "RegEdit_RegEdit"

    def test_elevated_beats_soft_allow_silent_paste(self, strategies):
        # An approved (process, class, control_type) tuple whose app
        # was relaunched as administrator must NOT silently paste --
        # the paste would be discarded by Windows and recorded as a
        # false success. Elevation wins over the soft-allow tier.
        soft_allow_predicate = _stub_predicate(TextTargetVerdict(
            verdict=True, reason="accept_soft_allow_tuple",
            control_type="Pane", class_name="Zed::Workspace",
            process_name="zed.exe",
        ))
        clipboard_only = MagicMock(name="ClipboardOnlyStrategy")
        router = self._router(
            strategies, checker=lambda ctrl: "elevated",
            predicate=soft_allow_predicate, clipboard_only=clipboard_only,
        )
        ctx = _focusable_ctx(process="zed.exe", class_name="Zed::Workspace")
        assert router.get_strategy(ctx, "hello") is strategies["rejected"]

    def test_elevated_beats_flutter_early_return(self, strategies):
        # wh-elevated-target-notice.1.1 (deepseek round 1): the Flutter
        # early return must not bypass the elevation check. UIPI
        # discards input by process integrity, not UI framework, so an
        # elevated Flutter app fails exactly like an elevated native
        # app and deserves the same notice.
        predicate = _accept_predicate()
        router = self._router(
            strategies, checker=lambda ctrl: "elevated",
            predicate=predicate,
        )
        ctx = _focusable_ctx(is_flutter=True)
        assert router.get_strategy(ctx, "hello") is strategies["rejected"]
        verdict = strategies["rejected"].set_pending_verdict.call_args[0][0]
        assert verdict.reason == "elevated_process_window"
        predicate.evaluate.assert_not_called()

    def test_not_elevated_flutter_keeps_flutter_strategy(self, strategies):
        router = self._router(strategies, checker=lambda ctrl: "not_elevated")
        ctx = _focusable_ctx(is_flutter=True)
        assert router.get_strategy(ctx, "hello") is strategies["flutter"]

    def test_unknown_flutter_keeps_flutter_strategy(self, strategies):
        router = self._router(strategies, checker=lambda ctrl: "unknown")
        ctx = _focusable_ctx(is_flutter=True)
        assert router.get_strategy(ctx, "hello") is strategies["flutter"]

    def test_not_elevated_takes_normal_path(self, strategies):
        router = self._router(strategies, checker=lambda ctrl: "not_elevated")
        ctx = _focusable_ctx()
        assert router.get_strategy(ctx, "hello") is strategies["verified_unicode"]

    def test_unknown_fails_open_to_normal_path(self, strategies):
        router = self._router(strategies, checker=lambda ctrl: "unknown")
        ctx = _focusable_ctx()
        assert router.get_strategy(ctx, "hello") is strategies["verified_unicode"]

    def test_checker_exception_fails_open(self, strategies):
        def _boom(ctrl):
            raise RuntimeError("win32 blew up")

        router = self._router(strategies, checker=_boom)
        ctx = _focusable_ctx()
        assert router.get_strategy(ctx, "hello") is strategies["verified_unicode"]

    def test_no_checker_wired_keeps_existing_behavior(self, strategies):
        # Legacy construction without the elevation_checker argument
        # must keep working (default None -> the check is skipped).
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
            simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=_accept_predicate(),
            verified_unicode_strategy=strategies["verified_unicode"],
            verified_unicode_max_chars=50,
        )
        ctx = _focusable_ctx()
        assert router.get_strategy(ctx, "hello") is strategies["verified_unicode"]

    def test_checker_receives_the_focused_control(self, strategies):
        seen = []

        def _checker(ctrl):
            seen.append(ctrl)
            return "not_elevated"

        router = self._router(strategies, checker=_checker)
        ctx = _focusable_ctx()
        router.get_strategy(ctx, "hello")
        assert seen == [ctx.focused_control]
