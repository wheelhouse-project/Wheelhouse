"""Phase 1 regression and unit tests for the soft-fallback path.

Covers wh-9weum Phase 1 (epic wh-1orea) -- the predicate's new branches,
the router's mapping to ClipboardOnlyStrategy, and the new strategy's
retry_outcome contract.

Specific bd children covered here:

  - wh-3ypov : default_reject_paste_capable_class reason name
  - wh-wmrbl : predicate emits the soft reason after hard rejects
  - wh-ko176 : EditControl ControlType is a positive accept signal
  - wh-jldm0 : wh-zndq browser empty-ClassName trap stays a hard reject
  - wh-pc28  : InsertionResult.retry_outcome contract
  - wh-0ci9n : ClipboardOnlyStrategy delivery + retry_outcome
  - wh-3oy1u : router maps the soft reason to ClipboardOnlyStrategy
  - wh-pcpuq : this file (regression suite)

Existing Phase 0 tests (test_text_target.py, test_insertion_router.py)
must keep passing -- run those alongside this file to verify Phase 1
did not regress wh-fc1x's safety baseline.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import uiautomation as auto

from ui.context import UIContext
from ui.router import InsertionRouter
from ui.strategies.base import InsertionResult
from ui.strategies.specific import ClipboardOnlyStrategy
from ui import text_target as _text_target_module
from ui.text_target import (
    DEFAULT_BROWSER_PROCESS_NAMES,
    TextTargetPredicate,
    TextTargetVerdict,
    build_predicate_from_config,
    is_text_target,
)


@pytest.fixture(autouse=True)
def _clear_default_predicate_soft_allow(monkeypatch):
    """Reset the module-level default predicate's soft-allow set.

    is_text_target() uses ui.text_target.default_predicate, which loads
    soft_allow_tuples.toml from disk at import time. The unit tests
    here assume an empty soft-allow set so the soft-reject branch of
    the predicate is exercised. A user smoke test or another test that
    writes the file pollutes that state for the rest of the run.
    Clearing the set before each test isolates these assertions from
    on-disk state.
    """
    monkeypatch.setattr(
        _text_target_module.default_predicate, "_soft_allow", frozenset(),
    )


# --- Helpers ---------------------------------------------------------------


def _ctrl(*, control_type=auto.ControlType.EditControl,
          control_type_name="EditControl",
          class_name="Edit",
          has_text_pattern=True,
          has_value_pattern=True,
          is_focusable=True,
          is_enabled=True):
    """Build a mock UIA control. Mirrors test_text_target.py's helper."""
    ctrl = MagicMock()
    ctrl.ControlType = int(control_type)
    ctrl.ControlTypeName = control_type_name
    ctrl.ClassName = class_name
    ctrl.IsKeyboardFocusable = is_focusable
    ctrl.IsEnabled = is_enabled

    def get_pattern(pid):
        if pid == auto.PatternId.TextPattern and has_text_pattern:
            return MagicMock(name="TextPattern")
        if pid == auto.PatternId.ValuePattern and has_value_pattern:
            return MagicMock(name="ValuePattern")
        return None

    ctrl.GetPattern.side_effect = get_pattern
    return ctrl


def _focusable_ctx(*, is_flutter=False, is_terminal=False, process="zed.exe",
                   class_name="ZedTextField"):
    ctrl = MagicMock()
    ctrl.IsKeyboardFocusable = True
    return UIContext(focused_control=ctrl, is_flutter=is_flutter,
                     is_terminal=is_terminal, process_name=process,
                     class_name=class_name)


# --- TestEditControlAccept (wh-ko176) --------------------------------------


class TestEditControlAccept:
    """EditControl ControlType accepts even when TextPattern is missing.

    Several real-world controls report ControlType=EditControl but only
    surface TextPattern on a delayed UIA query. The new accept branch
    avoids a soft fallback for those cases.
    """

    def test_edit_control_without_text_pattern_accepts(self):
        ctrl = _ctrl(control_type=auto.ControlType.EditControl,
                     control_type_name="EditControl",
                     class_name="CustomEdit",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="CustomEdit",
                           process_name="myapp.exe")
        assert v.verdict is True
        assert v.reason == "edit_control"
        assert v.control_type == "EditControl"

    def test_edit_control_with_text_pattern_still_uses_text_pattern(self):
        # When BOTH signals are present, text_pattern_available wins
        # because it is the canonical accept token. Order is important:
        # changing it would change telemetry semantics.
        ctrl = _ctrl(control_type=auto.ControlType.EditControl,
                     control_type_name="EditControl",
                     class_name="Edit",
                     has_text_pattern=True)
        v = is_text_target(ctrl, class_name="Edit")
        assert v.verdict is True
        assert v.reason == "text_pattern_available"

    def test_disabled_edit_control_does_not_accept(self):
        # IsEnabled is required for the EditControl accept signal.
        # A disabled edit field falls through to the soft-reject path
        # because ClassName is non-empty.
        ctrl = _ctrl(control_type=auto.ControlType.EditControl,
                     control_type_name="EditControl",
                     class_name="Edit",
                     has_text_pattern=False,
                     has_value_pattern=False,
                     is_enabled=False)
        v = is_text_target(ctrl, class_name="Edit",
                           process_name="myapp.exe")
        assert v.verdict is False
        assert v.reason == "default_reject_paste_capable_class"

    def test_document_control_alone_does_not_accept(self):
        # wh-sm5s.5: DocumentControl alone (no TextPattern) must NOT
        # accept; otherwise the wh-zndq browser-body case re-opens.
        ctrl = _ctrl(control_type=auto.ControlType.DocumentControl,
                     control_type_name="DocumentControl",
                     class_name="Chrome_RenderWidgetHostHWND",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="Chrome_RenderWidgetHostHWND",
                           process_name="brave.exe")
        assert v.verdict is False
        # Non-empty ClassName plus browser process and missing
        # TextPattern: the browser-empty-trap does NOT fire because
        # ClassName is non-empty, so the soft reject applies.
        assert v.reason == "default_reject_paste_capable_class"


# --- TestSoftReject (wh-wmrbl, wh-3ypov) -----------------------------------


class TestSoftReject:
    """Non-empty ClassName with no positive signal emits the soft reason.

    The router maps this reason to ClipboardOnlyStrategy. Editors that
    render their own UI (Zed, Sublime) hit this branch.
    """

    def test_zed_shape_emits_soft_reject(self):
        # Zed reports ControlType=WindowControl, ClassName="Zed::Window",
        # no TextPattern. Hard rejects do not fire.
        ctrl = _ctrl(control_type=auto.ControlType.WindowControl,
                     control_type_name="WindowControl",
                     class_name="Zed::Window",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="Zed::Window",
                           process_name="zed.exe")
        assert v.verdict is False
        assert v.reason == "default_reject_paste_capable_class"
        assert v.control_type == "WindowControl"
        assert v.class_name == "Zed::Window"

    def test_pane_control_with_unknown_class_soft_rejects(self):
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="WeirdEditor",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="WeirdEditor",
                           process_name="myapp.exe")
        assert v.verdict is False
        assert v.reason == "default_reject_paste_capable_class"

    def test_empty_class_non_browser_falls_through_to_default_reject(self):
        # Empty ClassName outside the browser list: nothing to soft-paste
        # into. Stays a hard default_reject. Documented behavior --
        # see predicate docstring step 11.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="",
                           process_name="myapp.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"

    def test_denylist_class_still_hard_rejects(self):
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="MenuFlyoutSubItem",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="MenuFlyoutSubItem",
                           process_name="notepad.exe")
        assert v.verdict is False
        assert v.reason == "denylist_class_name"

    def test_denylist_control_type_still_hard_rejects(self):
        ctrl = _ctrl(control_type=auto.ControlType.ButtonControl,
                     control_type_name="ButtonControl",
                     class_name="Button",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="Button",
                           process_name="myapp.exe")
        assert v.verdict is False
        assert v.reason == "denylist_control_type"

    def test_supported_patterns_carries_value_pattern_on_soft_reject(self):
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="ZedTextField",
                     has_text_pattern=False,
                     has_value_pattern=True)
        v = is_text_target(ctrl, class_name="ZedTextField",
                           process_name="zed.exe")
        assert v.verdict is False
        assert v.reason == "default_reject_paste_capable_class"
        assert "ValuePattern" in v.supported_patterns


# --- TestBrowserEmptyTrap (wh-jldm0) ---------------------------------------


class TestBrowserEmptyTrap:
    """The wh-zndq browser-body case must keep hard-rejecting.

    Soft fallback would cost an extra clipboard write per dictation
    word into a browser body that drops Ctrl+V silently. The trap
    fires on (browser process + empty ClassName + missing TextPattern).
    """

    def test_brave_empty_class_hard_rejects(self):
        ctrl = _ctrl(control_type=auto.ControlType.DocumentControl,
                     control_type_name="DocumentControl",
                     class_name="",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="",
                           process_name="brave.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"

    def test_chrome_empty_class_hard_rejects(self):
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="",
                           process_name="chrome.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"

    def test_msedge_empty_class_hard_rejects(self):
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="",
                           process_name="msedge.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"

    def test_firefox_empty_class_hard_rejects(self):
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="",
                           process_name="firefox.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"

    def test_browser_with_non_empty_class_uses_soft_reject(self):
        # The trap is specific to empty ClassName. A browser process
        # with a non-empty ClassName goes through the soft path so
        # editors hosted in browser processes (Electron-style apps in
        # Chromium) still get the Ctrl+V attempt.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="some-renderer-class",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="some-renderer-class",
                           process_name="brave.exe")
        assert v.verdict is False
        assert v.reason == "default_reject_paste_capable_class"

    def test_browser_with_text_pattern_and_empty_classname_still_rejects(self):
        # wh-fc1x.1: the trap now runs BEFORE the TextPattern probe.
        # A browser DocumentControl with empty ClassName is the page
        # body in real captures -- typing into it sends spaces that
        # Brave interprets as scroll-down, producing a page-down per
        # word. Real contenteditable elements carry a non-empty
        # ClassName (e.g. Chrome_RenderWidgetHostHWND, see
        # test_text_target.test_document_control_with_text_pattern_accepts)
        # so they pass the trap and accept at the TextPattern probe.
        ctrl = _ctrl(control_type=auto.ControlType.DocumentControl,
                     control_type_name="DocumentControl",
                     class_name="",
                     has_text_pattern=True)
        v = is_text_target(ctrl, class_name="",
                           process_name="brave.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"

    def test_browser_page_body_with_text_pattern_and_chrome_class_accepts(self):
        # wh-fc1x.1 regression: confirm a contenteditable shape with
        # the Chrome renderer ClassName still accepts. Captured shape
        # comes from the test_text_target Chromium contenteditable
        # fixture and a hypothetical page that surfaces the renderer
        # window class on the focused element.
        ctrl = _ctrl(control_type=auto.ControlType.DocumentControl,
                     control_type_name="DocumentControl",
                     class_name="Chrome_RenderWidgetHostHWND",
                     has_text_pattern=True)
        v = is_text_target(ctrl, class_name="Chrome_RenderWidgetHostHWND",
                           process_name="brave.exe")
        assert v.verdict is True
        assert v.reason == "text_pattern_available"

    def test_brave_address_bar_with_text_pattern_accepts(self):
        # wh-fc1x.1 regression: real-world capture from the user's log
        # at 2026-04-30 21:29:15 (services/wheelhouse/wheelhouse.log
        # line 2126). Brave's address bar reports
        # ControlType=EditControl + ClassName=BraveOmniboxViewViews +
        # TextPattern. The trap must let this pass and the TextPattern
        # accept must fire.
        ctrl = _ctrl(control_type=auto.ControlType.EditControl,
                     control_type_name="EditControl",
                     class_name="BraveOmniboxViewViews",
                     has_text_pattern=True)
        v = is_text_target(ctrl, class_name="BraveOmniboxViewViews",
                           process_name="brave.exe")
        assert v.verdict is True
        assert v.reason == "text_pattern_available"

    def test_google_search_box_with_text_pattern_accepts(self):
        # wh-fc1x.1 regression: real-world capture from the user's log
        # at 2026-04-30 21:30:11 (services/wheelhouse/wheelhouse.log
        # line 2303). The Google search box reports
        # ControlType=ComboBoxControl + ClassName=gLFyf + TextPattern.
        # The trap must let this pass and the TextPattern accept must
        # fire.
        ctrl = _ctrl(control_type=auto.ControlType.ComboBoxControl,
                     control_type_name="ComboBoxControl",
                     class_name="gLFyf",
                     has_text_pattern=True)
        v = is_text_target(ctrl, class_name="gLFyf",
                           process_name="brave.exe")
        assert v.verdict is True
        assert v.reason == "text_pattern_available"

    def test_default_browser_list_includes_known_browsers(self):
        # The hardcoded baseline must cover the four named in the
        # converged design: Brave, Chrome, MSEdge, Firefox. The
        # constant is exposed for direct testing so config typos
        # cannot silently shrink the list.
        assert "brave.exe" in DEFAULT_BROWSER_PROCESS_NAMES
        assert "chrome.exe" in DEFAULT_BROWSER_PROCESS_NAMES
        assert "msedge.exe" in DEFAULT_BROWSER_PROCESS_NAMES
        assert "firefox.exe" in DEFAULT_BROWSER_PROCESS_NAMES

    def test_process_name_match_is_case_insensitive(self):
        # Windows process names are case-insensitive in practice; the
        # predicate must not let a "Brave.exe" capture slip past the
        # trap when the baseline stores "brave.exe".
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="",
                           process_name="Brave.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"


# --- TestConfigBrowserExtension (wh-jldm0 config) --------------------------


class TestConfigBrowserExtension:
    """browser_process_names_extend adds processes to the trap.

    Extending the foreground-check list (a different config section)
    does NOT affect the predicate. The two lists are independent
    (review note wh-sm5s.4).
    """

    def test_extend_adds_process_to_trap(self):
        config = {
            "ui_actions": {
                "text_target": {
                    "browser_process_names_extend": ["arc.exe"],
                }
            }
        }
        predicate = build_predicate_from_config(config)
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = predicate.evaluate(ctrl, class_name="",
                               process_name="arc.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"

    def test_extend_does_not_remove_default_processes(self):
        # An empty extend list must keep the baseline trap intact.
        config = {
            "ui_actions": {
                "text_target": {
                    "browser_process_names_extend": [],
                }
            }
        }
        predicate = build_predicate_from_config(config)
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = predicate.evaluate(ctrl, class_name="",
                               process_name="brave.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"

    def test_extend_string_value_warns_and_ignores(self, caplog):
        config = {
            "ui_actions": {
                "text_target": {
                    "browser_process_names_extend": "arc.exe",
                }
            }
        }
        with caplog.at_level("WARNING"):
            predicate = build_predicate_from_config(config)
        assert any(
            "browser_process_names_extend" in r.message
            and "must be a list" in r.message
            for r in caplog.records
        )
        # Defaults preserved.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = predicate.evaluate(ctrl, class_name="",
                               process_name="brave.exe")
        assert v.reason == "default_reject"

    def test_foreground_check_extend_does_not_affect_predicate(self):
        # A test fixture that extends only the foreground-check list
        # should NOT make the predicate's trap fire on a non-baseline
        # process. Demonstrates the two lists are independent.
        # The predicate consumes only [ui_actions.text_target] -- it
        # does not look at [ui_actions.foreground_check] at all.
        config = {
            "ui_actions": {
                "foreground_check": {
                    "same_process_browser_names_extend": ["arc.exe"],
                },
                "text_target": {},
            }
        }
        predicate = build_predicate_from_config(config)
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = predicate.evaluate(ctrl, class_name="",
                               process_name="arc.exe")
        # arc.exe is NOT in the predicate's baseline browser list and
        # was extended only on the foreground-check side, so the trap
        # does not fire. Empty ClassName outside the browser list
        # falls through to default_reject.
        assert v.verdict is False
        assert v.reason == "default_reject"


# --- TestRouterSoftRejectMapping (wh-3oy1u) --------------------------------


class TestRouterSoftRejectMapping:
    """Router routing for the four-tier verdict.

    wh-soft-allow-verdict-tier split the old single soft-fallback path
    into two tiers:

      * Known soft-allow tuple -> verdict=True,
        reason='accept_soft_allow_tuple' -> ClipboardOnlyStrategy
        (silent paste, what wh-3oy1u set up for the known case).
      * Unknown paste-capable class -> verdict=False,
        reason='default_reject_paste_capable_class' ->
        RejectedInsertionStrategy (toast + Try-it-anyway override).

    Hard rejects (default_reject, denylists, etc.) continue to route to
    RejectedInsertionStrategy unchanged.
    """

    @pytest.fixture
    def strategies(self):
        return {
            "standard": MagicMock(name="StandardStrategy"),
            "flutter": MagicMock(name="FlutterStrategy"),
            "simple_paste": MagicMock(name="SimplePasteStrategy"),
            "rejected": MagicMock(name="RejectedInsertionStrategy"),
            "verified_unicode": MagicMock(name="VerifiedUnicodeStrategy"),
            "clipboard_only": MagicMock(name="ClipboardOnlyStrategy"),
        }

    @staticmethod
    def _stub_predicate(verdict):
        predicate = MagicMock(spec=TextTargetPredicate)
        predicate.evaluate.return_value = verdict
        return predicate

    def _router(self, strategies, predicate):
        return InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
            simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=predicate,
            verified_unicode_strategy=strategies["verified_unicode"],
            verified_unicode_max_chars=50,
            clipboard_only_strategy=strategies["clipboard_only"],
        )

    def test_accept_soft_allow_tuple_routes_to_clipboard_only(self, strategies):
        # The new accept tier: known soft-allow tuple -> ClipboardOnly.
        verdict = TextTargetVerdict(
            verdict=True,
            reason="accept_soft_allow_tuple",
            control_type="WindowControl", class_name="Zed::Window",
            process_name="zed.exe",
        )
        router = self._router(strategies, self._stub_predicate(verdict))
        ctx = _focusable_ctx(process="zed.exe", class_name="Zed::Window")
        assert router.get_strategy(ctx, "hello") is strategies["clipboard_only"]

    def test_accept_soft_allow_tuple_short_circuits_length_branch(self, strategies):
        # Even a long utterance routes to ClipboardOnly when the verdict
        # carries the soft-allow accept reason. The length-based default
        # branch is for the predicate's other accept reasons
        # (text_pattern_available, edit_control, class_name_allowlist).
        verdict = TextTargetVerdict(
            verdict=True,
            reason="accept_soft_allow_tuple",
            control_type="WindowControl", class_name="Zed::Window",
        )
        router = self._router(strategies, self._stub_predicate(verdict))
        ctx = _focusable_ctx(process="zed.exe", class_name="Zed::Window")
        assert router.get_strategy(ctx, "x" * 200) is strategies["clipboard_only"]

    def test_unknown_soft_reject_routes_to_rejected(self, strategies):
        # The bug the wh-prio bead surfaced: an unknown soft-reject must
        # NOT silently paste. It routes to RejectedInsertionStrategy so
        # the toast and Try-it-anyway button fire.
        verdict = TextTargetVerdict(
            verdict=False,
            reason="default_reject_paste_capable_class",
            control_type="WindowControl", class_name="Zed::Window",
            process_name="zed.exe",
        )
        router = self._router(strategies, self._stub_predicate(verdict))
        ctx = _focusable_ctx(process="zed.exe", class_name="Zed::Window")
        assert router.get_strategy(ctx, "hello") is strategies["rejected"]
        # Long text takes the same path -- the soft-reject reason
        # short-circuits the length branch.
        assert router.get_strategy(ctx, "x" * 200) is strategies["rejected"]

    def test_default_reject_still_routes_to_rejected(self, strategies):
        # Browser-empty trap returns reason='default_reject', and that
        # must NOT route to ClipboardOnly.
        verdict = TextTargetVerdict(
            verdict=False, reason="default_reject",
            control_type="DocumentControl", class_name="",
            process_name="brave.exe",
        )
        router = self._router(strategies, self._stub_predicate(verdict))
        ctx = _focusable_ctx(process="brave.exe", class_name="")
        assert router.get_strategy(ctx, "hello") is strategies["rejected"]

    def test_denylist_reject_still_routes_to_rejected(self, strategies):
        verdict = TextTargetVerdict(
            verdict=False, reason="denylist_control_type",
            control_type="ButtonControl", class_name="Button",
            process_name="myapp.exe",
        )
        router = self._router(strategies, self._stub_predicate(verdict))
        ctx = _focusable_ctx(process="myapp.exe", class_name="Button")
        assert router.get_strategy(ctx, "hello") is strategies["rejected"]

    def test_router_without_clipboard_only_falls_back_to_default(self, strategies):
        # Backwards-compatibility branch: an older fixture that builds
        # the router without ClipboardOnly should keep working. With
        # the wh-soft-allow-verdict-tier change, the soft-allow accept
        # falls through to the default length-based branch (the
        # legitimate accept handlers) instead of failing closed.
        router = InsertionRouter(
            standard_strategy=strategies["standard"],
            flutter_strategy=strategies["flutter"],
            simple_paste_strategy=strategies["simple_paste"],
            rejected_strategy=strategies["rejected"],
            text_target_predicate=self._stub_predicate(TextTargetVerdict(
                verdict=True,
                reason="accept_soft_allow_tuple",
                control_type="WindowControl", class_name="Zed::Window",
            )),
            verified_unicode_strategy=strategies["verified_unicode"],
            verified_unicode_max_chars=50,
            # clipboard_only_strategy intentionally omitted.
        )
        ctx = _focusable_ctx(process="zed.exe", class_name="Zed::Window")
        assert router.get_strategy(ctx, "hello") is strategies["verified_unicode"]


# --- TestClipboardOnlyStrategy (wh-0ci9n) ----------------------------------


class _FakeClipboard:
    """Stand-in for ClipboardOperations that models the counter increment.

    The production verified_paste advances accumulated_paste_chars by
    len(text) on every successful paste (clipboard_operations.py around
    line 426). Earlier versions of these tests used a bare MagicMock
    that never advanced the counter, which let a strategy bug
    (review wh-kox5.2) slip through unnoticed. This fake mirrors the
    real side effect so the strategy's snapshot/restore behavior is
    actually exercised.

    Configurable inputs:
      - verified_paste_returns: what verified_paste returns
      - keystroke_fires: whether last_paste_was_sent flips True
      - optimistic: value of last_paste_was_optimistic after the call
      - increments_counter_on_true: whether the success path advances
        accumulated_paste_chars (matches the production contract; only
        the True path increments)
    """

    def __init__(
        self,
        *,
        verified_paste_returns=True,
        keystroke_fires=True,
        optimistic=False,
        increments_counter_on_true=True,
        starting_counter=0,
    ):
        self._verified_paste_returns = verified_paste_returns
        self._keystroke_fires = keystroke_fires
        self._optimistic = optimistic
        self._increments_counter_on_true = increments_counter_on_true
        self.accumulated_paste_chars = starting_counter
        # wh-pkhrp.2: ClipboardOnlyStrategy now snapshots and restores
        # accumulated_paste_clusters and accumulated_paste_was_qt
        # alongside accumulated_paste_chars. wh-pkhrp.2.1.2 (codex
        # finding): the snapshot also covers accumulated_has_grapheme_unsafe.
        self.accumulated_paste_clusters = starting_counter
        self.accumulated_paste_was_qt = False
        self.accumulated_has_grapheme_unsafe = False
        self.last_paste_was_sent = False
        self.last_paste_was_optimistic = False
        self.calls: list[str] = []

    def verified_paste(self, text, *_args, **_kwargs):
        # Mirror production behavior: reset both flags at entry
        # (wh-d43oi), set last_paste_was_sent immediately before
        # the keystroke would fire, then return.
        self.last_paste_was_sent = False
        self.last_paste_was_optimistic = False
        self.calls.append(text)
        if self._keystroke_fires:
            self.last_paste_was_sent = True
        if self._verified_paste_returns:
            self.last_paste_was_optimistic = self._optimistic
            if self._increments_counter_on_true:
                self.accumulated_paste_chars += len(text)
                # wh-pkhrp.2: mirror the cluster counter advance.
                # The fake does not exercise grapheme-cluster cases;
                # ASCII text gives clusters == len anyway.
                self.accumulated_paste_clusters += len(text)
        return self._verified_paste_returns


class TestClipboardOnlyStrategy:
    """ClipboardOnlyStrategy delivers via verified_paste and emits retry_outcome.

    Verified vs unverified is decided together with success based on
    verified_paste's return AND the keystroke / optimistic flags.

      verified_paste True, not optimistic        -> success+verified
      verified_paste True, optimistic            -> success+unverified
      verified_paste False, keystroke fired      -> success+unverified
      verified_paste False, no keystroke         -> failure+unverified
    """

    def _ctx(self):
        ctrl = MagicMock()
        # Provide a top-level control with NativeWindowHandle so
        # _hwnd_from_control returns a value.
        top = MagicMock()
        top.NativeWindowHandle = 0x12345
        ctrl.GetTopLevelControl.return_value = top
        return UIContext(focused_control=ctrl, is_flutter=False,
                         is_terminal=False, process_name="zed.exe",
                         class_name="Zed::Window")

    def test_verified_paste_success_returns_verified(self):
        clipboard = _FakeClipboard(
            verified_paste_returns=True, optimistic=False,
        )
        # text_perfector=None makes DICTATION fall back to verbatim
        # delivery, which is enough for the existing assertions; the
        # new TestStreamedDictationPerfecting class exercises the
        # perfecting path explicitly.
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=None,
        )
        result = strategy.insert("hello world", self._ctx())
        assert isinstance(result, InsertionResult)
        assert result.success is True
        assert result.retry_outcome == "verified"
        assert result.clipboard_dirty is True

    def test_verified_paste_optimistic_returns_unverified(self):
        # verified_paste returns True but it took the optimistic path
        # (clipboard verification timed out without observing wrong
        # content). For the override-counter contract this is
        # unverified -- we did not observe positive confirmation.
        clipboard = _FakeClipboard(
            verified_paste_returns=True, optimistic=True,
        )
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=None,
        )
        result = strategy.insert("hello", self._ctx())
        assert result.success is True
        assert result.retry_outcome == "unverified"

    def test_post_send_failure_returns_success_unverified(self):
        # Review wh-kox5.1: verified_paste returns False because the
        # post-send foreground check failed, but Ctrl+V already fired
        # (last_paste_was_sent is True). The wh-pc28 contract says the
        # handler must NOT raise on success+unverified, so the strategy
        # returns success=True with retry_outcome="unverified". The
        # caller's Future resolves cleanly.
        clipboard = _FakeClipboard(
            verified_paste_returns=False,
            keystroke_fires=True,
            optimistic=False,
        )
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=None,
        )
        result = strategy.insert("hello", self._ctx())
        assert result.success is True
        assert result.retry_outcome == "unverified"
        assert result.clipboard_dirty is True

    def test_pre_send_failure_returns_failure_unverified(self):
        # _safe_copy or clipboard verification refused before any
        # keystroke. last_paste_was_sent stays False; the IPC did not
        # succeed, so the caller's Future surfaces the failure.
        clipboard = _FakeClipboard(
            verified_paste_returns=False,
            keystroke_fires=False,
            optimistic=False,
        )
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=None,
        )
        result = strategy.insert("hello", self._ctx())
        assert result.success is False
        assert result.retry_outcome == "unverified"
        # clipboard_dirty stays True even on failure -- a failed
        # verified_paste can still leave dictated text on the clipboard.
        assert result.clipboard_dirty is True

    def test_strategy_does_not_advance_paste_counter_on_success(self):
        # Review wh-kox5.2: production verified_paste advances
        # accumulated_paste_chars on success. ClipboardOnly must NOT
        # leak that increment because the soft-paste's delivery is
        # not proven and a later retract would walk over the wrong
        # span. The strategy snapshots and restores around the call.
        clipboard = _FakeClipboard(
            verified_paste_returns=True,
            optimistic=False,
            increments_counter_on_true=True,
            starting_counter=42,
        )
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=None,
        )
        strategy.insert("hello world", self._ctx())
        # 42 must stay 42 even though verified_paste tried to add 11.
        assert clipboard.accumulated_paste_chars == 42

    def test_strategy_does_not_advance_paste_counter_on_optimistic(self):
        # Optimistic path returns True but the strategy still snapshot/
        # restores the counter. (Production verified_paste does not
        # advance the counter on the optimistic timeout path -- it
        # returns True before reaching line 426 -- but the snapshot/
        # restore is robust to either contract.)
        clipboard = _FakeClipboard(
            verified_paste_returns=True,
            optimistic=True,
            increments_counter_on_true=True,
            starting_counter=7,
        )
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=None,
        )
        strategy.insert("hello", self._ctx())
        assert clipboard.accumulated_paste_chars == 7

    def test_strategy_restores_grapheme_unsafe_flag_on_success(self):
        # wh-pkhrp.2.1.2 (codex finding): the snapshot must cover
        # accumulated_has_grapheme_unsafe too. A soft-paste containing
        # ZWJ or surrogate-pair text would otherwise leak the flag
        # into the retract accounting even though the strategy is
        # designed to stay out of retract accounting.
        class _FakeUnsafeClipboard(_FakeClipboard):
            def verified_paste(self, text, *_args, **_kwargs):
                super().verified_paste(text, *_args, **_kwargs)
                # Pretend the inserted text was grapheme-unsafe.
                self.accumulated_has_grapheme_unsafe = True
                return self._verified_paste_returns

        clipboard = _FakeUnsafeClipboard(
            verified_paste_returns=True, optimistic=False,
        )
        clipboard.accumulated_has_grapheme_unsafe = False
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=None,
        )
        strategy.insert("hello world", self._ctx())
        assert clipboard.accumulated_has_grapheme_unsafe is False

    def test_strategy_restores_fields_when_verified_paste_raises(self):
        # wh-pkhrp.2.1.2 (codex finding): the restore lives in a
        # finally block so a raise from verified_paste does not leak
        # mutations into the retract accounting.
        class _RaisingClipboard(_FakeClipboard):
            def verified_paste(self, text, *_args, **_kwargs):
                # Mutate the fields, then raise. The strategy's
                # finally block must roll the mutations back.
                self.accumulated_paste_chars += len(text)
                self.accumulated_paste_clusters += len(text)
                self.accumulated_paste_was_qt = True
                self.accumulated_has_grapheme_unsafe = True
                raise RuntimeError("simulated paste failure")

        clipboard = _RaisingClipboard(starting_counter=42)
        clipboard.accumulated_has_grapheme_unsafe = False
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=None,
        )
        with pytest.raises(RuntimeError, match="simulated paste failure"):
            strategy.insert("hello world", self._ctx())
        assert clipboard.accumulated_paste_chars == 42
        assert clipboard.accumulated_paste_clusters == 42
        assert clipboard.accumulated_paste_was_qt is False
        assert clipboard.accumulated_has_grapheme_unsafe is False


# --- TestStreamedDictationPerfecting (live-fix regression) -----------------


class TestStreamedDictationPerfecting:
    """Streamed dictation through ClipboardOnly must produce spaces and
    sentence-start capitalization.

    Reproduces the live bug observed when dictating into Zed: words
    were pasted end-to-end with no separator (e.g. "helloworld"). The
    fix runs TextPerfector against a per-utterance preceding-chars
    mirror inside the strategy.
    """

    def _ctx(self):
        ctrl = MagicMock()
        top = MagicMock()
        top.NativeWindowHandle = 0x12345
        ctrl.GetTopLevelControl.return_value = top
        return UIContext(focused_control=ctrl, is_flutter=False,
                         is_terminal=False, process_name="zed.exe",
                         class_name="Zed::Window")

    def test_streamed_words_get_leading_space(self):
        # Stream three words, capture each text-to-paste seen by
        # verified_paste. The first word starts the sentence; the
        # second and third should have a leading space.
        clipboard = _FakeClipboard(
            verified_paste_returns=True, optimistic=False,
        )
        from ui.text_perfector import TextPerfector
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=TextPerfector(),
        )
        ctx = self._ctx()
        strategy.insert("hello", ctx)
        strategy.insert("world", ctx)
        strategy.insert("again", ctx)
        # The captured calls list records every text passed to
        # verified_paste.
        assert clipboard.calls == ["Hello", " world", " again"]

    def test_reset_preceding_mirror_starts_new_sentence(self):
        # First utterance ends with "Hello world". After
        # reset_preceding_mirror, the next utterance must capitalize
        # its first word again.
        clipboard = _FakeClipboard(
            verified_paste_returns=True, optimistic=False,
        )
        from ui.text_perfector import TextPerfector
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=TextPerfector(),
        )
        ctx = self._ctx()
        strategy.insert("hello", ctx)
        strategy.insert("world", ctx)
        strategy.reset_preceding_mirror()
        strategy.insert("again", ctx)
        # "Hello", " world", then a fresh "Again" -- sentence-start
        # capitalization, no leading space.
        assert clipboard.calls == ["Hello", " world", "Again"]

    def test_verbatim_mode_skips_perfecter(self):
        # VERBATIM-mode callers (selection-wrap, transform_selection)
        # pass the final composed text. The strategy must not modify
        # it. The mirror still updates so a later DICTATION word in
        # the same utterance perfects against the right context.
        from ui.strategies.base import InsertionMode, InsertionOptions
        from ui.text_perfector import TextPerfector
        clipboard = _FakeClipboard(
            verified_paste_returns=True, optimistic=False,
        )
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=TextPerfector(),
        )
        ctx = self._ctx()
        verbatim_opts = InsertionOptions(mode=InsertionMode.VERBATIM)
        strategy.insert("EXACTLY THIS", ctx, options=verbatim_opts)
        # Verbatim text lands as-is.
        assert clipboard.calls == ["EXACTLY THIS"]
        # A subsequent dictation word perfects against the verbatim
        # text in the mirror (preceding ends with "S", non-whitespace,
        # so a leading space is added).
        strategy.insert("more", ctx)
        assert clipboard.calls == ["EXACTLY THIS", " more"]

    def test_post_send_failure_still_advances_mirror(self):
        # When the keystroke fires but verified_paste returns False
        # (post-send foreground check failed), the paste may have
        # landed. The mirror must still advance so the next word does
        # not double-perfect against a stale shorter preceding string.
        clipboard = _FakeClipboard(
            verified_paste_returns=False,
            keystroke_fires=True,
            optimistic=False,
        )
        from ui.text_perfector import TextPerfector
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=TextPerfector(),
        )
        ctx = self._ctx()
        strategy.insert("hello", ctx)
        # Even though verified_paste returned False, the mirror got
        # "Hello" so the next word gets a leading space.
        strategy.insert("world", ctx)
        assert clipboard.calls == ["Hello", " world"]

    def test_pre_send_failure_does_not_advance_mirror(self):
        # When no keystroke fires (pre-send failure: _safe_copy
        # refused), the paste did not land. The mirror must NOT
        # advance, so the next word still perfects as if from a
        # fresh start.
        clipboard = _FakeClipboard(
            verified_paste_returns=False,
            keystroke_fires=False,
            optimistic=False,
        )
        from ui.text_perfector import TextPerfector
        strategy = ClipboardOnlyStrategy(
            clipboard, MagicMock(), text_perfector=TextPerfector(),
        )
        ctx = self._ctx()
        strategy.insert("hello", ctx)
        # Pre-send failed; mirror stayed empty. Next word capitalizes.
        strategy.insert("world", ctx)
        # Both calls show what was passed to verified_paste.
        # First: "Hello" (sentence-start, mirror empty, did not advance).
        # Second: "World" (still sentence-start because mirror is empty).
        assert clipboard.calls == ["Hello", "World"]


# --- TestHandlerBufferInvalidationOnClipboardOnly (wh-kox5.3) --------------


class TestHandlerBufferInvalidationOnClipboardOnly:
    """The handler must invalidate the shadow buffer when the router
    selects ClipboardOnly.

    Without this invalidation, an utterance that interleaves Standard
    (writes valid buffer), then ClipboardOnly (changes target without
    updating buffer), then Standard would compose against stale
    preceding chars and corrupt spacing/capitalization. The fix lives
    in the handler because the buffer_manager is a handler-owned
    component and the strategy intentionally has no reference to it.
    """

    def _build_handler_with_clipboard_only_routed(self):
        from unittest.mock import patch
        # Build the handler with all dependencies mocked, then swap
        # the router so get_strategy returns the ClipboardOnly
        # instance the handler constructed.
        _MOD = "ui.ui_action_handler"
        with patch(f"{_MOD}.TextPerfector"), \
             patch(f"{_MOD}.ClipboardOperations"), \
             patch(f"{_MOD}.WindowFocusManager"), \
             patch(f"{_MOD}.SelectionTransformer"), \
             patch(f"{_MOD}.UtteranceClipboardManager"), \
             patch(f"{_MOD}.ShadowBufferManager") as MockSBM, \
             patch(f"{_MOD}.TerminalEditorProxy"), \
             patch(f"{_MOD}.InsertionRouter"), \
             patch(f"{_MOD}.capture_context") as MockCC:

            from ui.ui_action_handler import UIActionHandler

            cfg = {
                "ui_actions": {
                    "timing": {
                        "utterance_clipboard_timeout_seconds": 1.0,
                    },
                    "verified_unicode": {"max_chars": 50},
                    "foreground_check": {
                        "same_process_browser_names_extend": [],
                    },
                    "text_target": {},
                }
            }
            handler = UIActionHandler(response_queue=MagicMock(), config=cfg)

            # Wire capture_context to return a benign UIContext so the
            # _execute_insert_with_ack code path runs without raising.
            ctx_ctrl = MagicMock()
            top = MagicMock()
            top.NativeWindowHandle = 0x1234
            ctx_ctrl.GetTopLevelControl.return_value = top
            MockCC.return_value = UIContext(
                focused_control=ctx_ctrl, is_flutter=False,
                is_terminal=False, process_name="zed.exe",
                class_name="Zed::Window",
            )

            # Force the router to return the actual ClipboardOnly
            # instance the handler holds. The strategy itself reports
            # success+verified so the rest of _execute_insert_with_ack
            # walks the success branch where the buffer_manager.invalidate
            # call lives. Mock the strategy.insert directly so we do
            # not need to model verified_paste here.
            success_result = InsertionResult(
                success=True, clipboard_dirty=True,
                retry_outcome="verified",
            )
            handler.clipboard_only_strategy.insert = MagicMock(
                return_value=success_result,
            )
            handler.router.get_strategy = MagicMock(
                return_value=handler.clipboard_only_strategy,
            )

            return handler, MockSBM

    def test_clipboard_only_invalidates_buffer(self):
        handler, MockSBM = self._build_handler_with_clipboard_only_routed()
        # MockSBM is the patched class; MockSBM.return_value is the
        # MagicMock instance the handler holds as buffer_manager.
        # Use the class-level MagicMock for assertions because Pyright
        # cannot prove the attribute on the typed ShadowBufferManager
        # field; the runtime objects are identical.
        buffer_mock = MockSBM.return_value
        assert handler.buffer_manager is buffer_mock
        handler._execute_insert_with_ack(
            "hello world", request_id="r1",
        )
        buffer_mock.invalidate.assert_called()


# --- TestLatencyBaselineArtifact (wh-kox5.4) -------------------------------


class TestLatencyBaselineArtifact:
    """The latency baseline JSON must be present and structurally complete.

    Without a smoke check on the artifact, the env-gated capture path
    can drift (renamed scenario keys, dropped fields) without anyone
    noticing until Phase 5 (wh-mm39e) runs the regression comparison
    and fails for a structural reason rather than a real perf
    regression.
    """

    def test_baseline_file_exists(self):
        from pathlib import Path
        baseline = (
            Path(__file__).parent / "baselines" / "text_target_latency.json"
        )
        assert baseline.is_file(), (
            f"latency baseline missing at {baseline}; capture with "
            "WHEELHOUSE_LATENCY_BASELINE=1 uv run pytest "
            "tests/test_ui/test_text_target_latency.py and commit "
            "the result."
        )

    def test_baseline_schema_has_expected_scenarios(self):
        import json
        from pathlib import Path
        baseline = (
            Path(__file__).parent / "baselines" / "text_target_latency.json"
        )
        data = json.loads(baseline.read_text(encoding="utf-8"))
        assert "scenarios" in data
        expected_keys = {
            "text_pattern_accept",
            "edit_control_accept",
            "soft_reject_paste_capable_class",
            "browser_empty_class_trap",
            "browser_edit_control_exemption_accept",
            "default_reject_empty_class_non_browser",
            "denylist_control_type",
        }
        assert set(data["scenarios"].keys()) == expected_keys
        for name, stats in data["scenarios"].items():
            assert "mean_us" in stats, name
            assert stats["mean_us"] > 0, name


# --- TestInsertionResultRetryOutcome (wh-pc28) -----------------------------


class TestInsertionResultRetryOutcome:
    """The retry_outcome field defaults to 'n/a' for all non-ClipboardOnly
    strategies and is part of InsertionResult's frozen contract.
    """

    def test_default_retry_outcome_is_na(self):
        result = InsertionResult(success=True, clipboard_dirty=False)
        assert result.retry_outcome == "n/a"

    def test_retry_outcome_persists_when_set(self):
        result = InsertionResult(
            success=True, clipboard_dirty=True, retry_outcome="verified",
        )
        assert result.retry_outcome == "verified"

    def test_was_rejected_false_when_no_rejected_reason(self):
        result = InsertionResult(
            success=True, clipboard_dirty=False, retry_outcome="verified",
        )
        assert result.was_rejected is False

    def test_retry_outcome_is_independent_of_rejected_reason(self):
        # rejected_reason is for RejectedInsertionStrategy; retry_outcome
        # is for ClipboardOnlyStrategy. The two fields can coexist on
        # the dataclass even though no strategy sets both at once.
        result = InsertionResult(
            success=True, clipboard_dirty=False,
            rejected_reason="no_text_target", retry_outcome="n/a",
        )
        assert result.was_rejected is True
        assert result.retry_outcome == "n/a"
