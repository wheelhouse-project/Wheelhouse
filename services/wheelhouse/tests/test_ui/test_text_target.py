"""Tests for ui.text_target.

Covers wh-zndq (no-text-input dictation routing), wh-32d (backspace sent
to non-text elements), and the agreed design from review epic wh-ix1z
round 1 (codex-review-loop design pass).

Test grouping mirrors the rules in TextTargetPredicate.evaluate:

- TestPositiveAccept: TextPattern present -> accept regardless of
  ControlType (Tkinter Entry, contenteditable div, etc.).
- TestDenylist: ControlType denylist -> reject before TextPattern probe
  (so a ButtonControl with TextPattern is still rejected).
- TestClassNameDenylist: ClassName denylist (WinUI 3 MenuFlyoutSubItem)
  -> reject.
- TestValuePatternOnly: ValuePattern alone is NOT an accept signal.
- TestStaleControl: COM/OSError on identity read -> reject silently.
- TestAllowlist: class_name_allowlist accepts when populated.
- TestDefaultReject: no positive signal -> reject.
- TestConfigFactory: build_predicate_from_config extends defaults.
"""
import _ctypes
from unittest.mock import MagicMock, PropertyMock

import pytest
import uiautomation as auto

from ui.text_target import (
    DEFAULT_ALLOWLIST_CLASS_NAMES,
    DEFAULT_BROWSER_PROCESS_NAMES,
    TextTargetPredicate,
    build_predicate_from_config,
    is_text_target,
)


# --- Helpers ---------------------------------------------------------------


def _ctrl(*, control_type=auto.ControlType.EditControl,
          control_type_name="EditControl",
          class_name="Edit",
          has_text_pattern=True,
          has_value_pattern=True,
          is_focusable=True,
          is_enabled=True):
    """Build a mock UIA control for predicate tests."""
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


# --- TestPositiveAccept ----------------------------------------------------


class TestPositiveAccept:
    def test_edit_control_with_text_pattern_accepts(self):
        ctrl = _ctrl(control_type=auto.ControlType.EditControl,
                     control_type_name="EditControl", class_name="Edit")
        v = is_text_target(ctrl, class_name="Edit", process_name="brave.exe")
        assert v.verdict is True
        assert v.reason == "text_pattern_available"
        assert "TextPattern" in v.supported_patterns
        assert v.control_type == "EditControl"
        assert v.process_name == "brave.exe"

    def test_document_control_with_text_pattern_accepts(self):
        # Chromium contenteditable div -> DocumentControl + TextPattern.
        ctrl = _ctrl(control_type=auto.ControlType.DocumentControl,
                     control_type_name="DocumentControl",
                     class_name="Chrome_RenderWidgetHostHWND")
        v = is_text_target(ctrl, class_name="Chrome_RenderWidgetHostHWND")
        assert v.verdict is True
        assert v.reason == "text_pattern_available"

    def test_unknown_control_type_with_text_pattern_accepts(self):
        # Tkinter Entry: ClassName=TkChild, TextPattern present
        # (wh-zndq comment 5: TextPattern is the right discriminator).
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="TkChild")
        v = is_text_target(ctrl, class_name="TkChild", process_name="python.exe")
        assert v.verdict is True
        assert v.reason == "text_pattern_available"

    def test_text_pattern_present_value_pattern_absent_accepts(self):
        ctrl = _ctrl(has_text_pattern=True, has_value_pattern=False)
        v = is_text_target(ctrl)
        assert v.verdict is True
        assert "TextPattern" in v.supported_patterns
        assert "ValuePattern" not in v.supported_patterns


# --- TestDenylist ----------------------------------------------------------


class TestDenylist:
    def test_menu_item_rejected(self):
        # Notepad WinUI 3 menu item: MenuItemControl ControlType.
        ctrl = _ctrl(control_type=auto.ControlType.MenuItemControl,
                     control_type_name="MenuItemControl",
                     class_name="MenuItem", has_text_pattern=False)
        v = is_text_target(ctrl, class_name="MenuItem",
                           process_name="notepad.exe")
        assert v.verdict is False
        assert v.reason == "denylist_control_type"
        assert v.control_type == "MenuItemControl"

    def test_list_item_rejected_explorer_uiitem(self):
        # Windows Explorer file icon: ListItemControl + ClassName="UIItem".
        ctrl = _ctrl(control_type=auto.ControlType.ListItemControl,
                     control_type_name="ListItemControl",
                     class_name="UIItem", has_text_pattern=False)
        v = is_text_target(ctrl, class_name="UIItem",
                           process_name="explorer.exe")
        assert v.verdict is False
        assert v.reason == "denylist_control_type"

    def test_button_rejected_even_with_text_pattern(self):
        # ControlType denylist runs BEFORE TextPattern probe so a button
        # that misleadingly exposes TextPattern is still rejected.
        ctrl = _ctrl(control_type=auto.ControlType.ButtonControl,
                     control_type_name="ButtonControl",
                     class_name="Button", has_text_pattern=True)
        v = is_text_target(ctrl, class_name="Button")
        assert v.verdict is False
        assert v.reason == "denylist_control_type"

    def test_image_rejected(self):
        ctrl = _ctrl(control_type=auto.ControlType.ImageControl,
                     control_type_name="ImageControl",
                     class_name="", has_text_pattern=False)
        v = is_text_target(ctrl)
        assert v.verdict is False
        assert v.reason == "denylist_control_type"

    def test_tab_item_rejected(self):
        ctrl = _ctrl(control_type=auto.ControlType.TabItemControl,
                     control_type_name="TabItemControl",
                     class_name="TabItem", has_text_pattern=False)
        v = is_text_target(ctrl)
        assert v.verdict is False
        assert v.reason == "denylist_control_type"

    def test_tree_item_rejected(self):
        ctrl = _ctrl(control_type=auto.ControlType.TreeItemControl,
                     control_type_name="TreeItemControl",
                     class_name="TreeItem", has_text_pattern=False)
        v = is_text_target(ctrl)
        assert v.verdict is False
        assert v.reason == "denylist_control_type"


# --- TestClassNameDenylist -------------------------------------------------


class TestClassNameDenylist:
    def test_menu_flyout_sub_item_rejected_by_class_name(self):
        # WinUI 3 menu sub-item: ClassName denylist fires before
        # ControlType denylist so we still reject if UIA reported a
        # generic ControlType.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="MenuFlyoutSubItem", has_text_pattern=False)
        v = is_text_target(ctrl, class_name="MenuFlyoutSubItem",
                           process_name="notepad.exe")
        assert v.verdict is False
        assert v.reason == "denylist_class_name"

    def test_class_name_denylist_uses_control_class_when_context_empty(self):
        # If the captured UIContext class_name is empty, the predicate
        # uses focused_control.ClassName for the deny check.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="MenuFlyoutSubItem", has_text_pattern=False)
        v = is_text_target(ctrl, class_name="", process_name="notepad.exe")
        assert v.verdict is False
        assert v.reason == "denylist_class_name"


class TestNoClassNameInheritance:
    """wh-ix1z.11 fix: the predicate must not let the class_name
    parameter inherit allow / deny matches onto a freshly recaptured
    control whose own ClassName is empty. The class_name parameter is
    a telemetry hint only.
    """

    def test_empty_focused_class_does_not_inherit_caller_class_for_denylist(self):
        # The recaptured control has empty ClassName but the caller
        # passes class_name="MenuFlyoutSubItem" (the original captured
        # class). The predicate must NOT match denylist_class_name
        # because the focused_control's own ClassName is empty.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="", has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="MenuFlyoutSubItem",
                           process_name="notepad.exe")
        # No denylist match -- falls through to default_reject because
        # there is no TextPattern and no allowlist match either.
        assert v.verdict is False
        assert v.reason == "default_reject"

    def test_empty_focused_class_does_not_inherit_caller_class_for_allowlist(self):
        # The freshly captured control has empty ClassName, but the
        # caller's class_name parameter happens to match a class on the
        # allowlist of a custom predicate. Without the fix this would
        # falsely accept the new control.
        predicate = TextTargetPredicate(
            allowlist_class_names={"MyCustomEdit"},
        )
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="", has_text_pattern=False)
        v = predicate.evaluate(ctrl, class_name="MyCustomEdit")
        assert v.verdict is False
        assert v.reason == "default_reject"

    def test_telemetry_class_falls_back_to_caller_when_focused_empty(self):
        # The class_name parameter still flows into verdict.class_name
        # for telemetry when focused_control.ClassName is empty -- only
        # the deny / allow CHECK is gated on focused_control.ClassName.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="", has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="some-telemetry-hint",
                           process_name="notepad.exe")
        assert v.verdict is False
        assert v.class_name == "some-telemetry-hint"


# --- TestValuePatternOnly --------------------------------------------------


class TestValuePatternOnly:
    def test_value_pattern_alone_rejected(self):
        # A ValuePattern-bearing read-only control without TextPattern
        # must be rejected. The CheckBox ControlType also lands on the
        # denylist in this case; either denylist_control_type or
        # default_reject is acceptable as long as the verdict is False.
        ctrl = _ctrl(control_type=auto.ControlType.CheckBoxControl,
                     control_type_name="CheckBoxControl",
                     has_text_pattern=False, has_value_pattern=True)
        v = is_text_target(ctrl)
        assert v.verdict is False
        assert v.reason in ("denylist_control_type", "default_reject")

    def test_value_pattern_only_unknown_control_type_default_reject(self):
        # ValuePattern alone on a non-denylisted ControlType still
        # rejects.
        #
        # wh-9weum Phase 1 routing change: a non-empty ClassName with
        # no positive accept signal now emits the soft reason
        # default_reject_paste_capable_class so the router can route
        # the case to ClipboardOnlyStrategy. The original assertion
        # ("default_reject") covered the empty-ClassName case AND the
        # non-empty-ClassName case under one token; Phase 1 splits the
        # two so the test is updated to reflect the new contract.
        # The intent of this test (ValuePattern alone does NOT accept)
        # is preserved: verdict is still False.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="SomeUnknownClass",
                     has_text_pattern=False, has_value_pattern=True)
        v = is_text_target(ctrl, class_name="SomeUnknownClass")
        assert v.verdict is False
        assert v.reason == "default_reject_paste_capable_class"
        assert "ValuePattern" in v.supported_patterns
        assert "TextPattern" not in v.supported_patterns


class TestValuePatternProbeSkippedOnAccept:
    """wh-ix1z.8 latency mitigation: the predicate skips the ValuePattern
    probe when TextPattern is present (the accept path). Cuts roughly half
    the predicate's UIA-call cost on the common path. ValuePattern is
    still probed on the reject path so the rejection telemetry retains
    the supported_patterns detail.
    """

    def test_accept_path_does_not_probe_value_pattern(self):
        ctrl = _ctrl(has_text_pattern=True, has_value_pattern=True)
        is_text_target(ctrl)
        called_pids = [c.args[0] for c in ctrl.GetPattern.call_args_list]
        # TextPattern should be probed; ValuePattern must NOT be probed
        # on the accept path.
        assert auto.PatternId.TextPattern in called_pids
        assert auto.PatternId.ValuePattern not in called_pids

    def test_reject_path_does_probe_value_pattern_for_telemetry(self):
        # Default reject case (unknown class, no TextPattern, no
        # denylist hit) probes ValuePattern so the telemetry token
        # captures it.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="SomeUnknownClass",
                     has_text_pattern=False, has_value_pattern=True)
        v = is_text_target(ctrl, class_name="SomeUnknownClass")
        assert v.verdict is False
        called_pids = [c.args[0] for c in ctrl.GetPattern.call_args_list]
        assert auto.PatternId.ValuePattern in called_pids
        assert "ValuePattern" in v.supported_patterns

    def test_accept_path_supported_patterns_is_text_pattern_only(self):
        ctrl = _ctrl(has_text_pattern=True, has_value_pattern=True)
        v = is_text_target(ctrl)
        assert v.verdict is True
        assert v.supported_patterns == ("TextPattern",)


# --- TestStaleControl ------------------------------------------------------


class TestStaleControl:
    def test_no_focused_control_rejects(self):
        v = is_text_target(None, class_name="", process_name="brave.exe")
        assert v.verdict is False
        assert v.reason == "no_focused_control"
        assert v.process_name == "brave.exe"

    def test_com_error_on_control_type_returns_stale_com(self):
        ctrl = MagicMock()
        type(ctrl).ControlType = PropertyMock(
            side_effect=_ctypes.COMError(
                -2147220991,
                "An event was unable to invoke any of the subscribers",
                (None, None, None, 0, None),
            )
        )
        v = is_text_target(ctrl, class_name="", process_name="test.exe")
        assert v.verdict is False
        assert v.reason == "stale_com"

    def test_os_error_on_focusable_returns_stale_com(self):
        ctrl = MagicMock()
        ctrl.ControlType = int(auto.ControlType.EditControl)
        ctrl.ControlTypeName = "EditControl"
        ctrl.ClassName = "Edit"
        type(ctrl).IsKeyboardFocusable = PropertyMock(
            side_effect=OSError("access denied"),
        )
        v = is_text_target(ctrl, class_name="Edit")
        assert v.verdict is False
        assert v.reason == "stale_com"

    def test_not_focusable_rejects(self):
        ctrl = _ctrl(is_focusable=False)
        v = is_text_target(ctrl)
        assert v.verdict is False
        assert v.reason == "not_focusable"


# --- TestAllowlist ---------------------------------------------------------


class TestAllowlist:
    def test_allowlist_class_accepts_without_text_pattern(self):
        # Custom predicate with an explicit allowlist entry. The
        # ControlType is not on the denylist and TextPattern is absent;
        # the class allowlist match is the deciding signal.
        predicate = TextTargetPredicate(
            allowlist_class_names={"MyCustomEdit"},
        )
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="MyCustomEdit", has_text_pattern=False)
        v = predicate.evaluate(ctrl, class_name="MyCustomEdit",
                               process_name="myapp.exe")
        assert v.verdict is True
        assert v.reason == "class_name_allowlist"

    def test_default_allowlist_is_empty(self):
        # No silent broad acceptance from a hardcoded list.
        assert DEFAULT_ALLOWLIST_CLASS_NAMES == frozenset()


# --- TestDefaultReject -----------------------------------------------------


class TestDefaultReject:
    def test_no_positive_signal_default_rejects(self):
        # wh-9weum Phase 1: non-empty ClassName with no positive accept
        # signal now emits the soft reason. Empty-ClassName cases
        # (covered by other tests) still emit default_reject.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="UnknownClass",
                     has_text_pattern=False, has_value_pattern=False)
        v = is_text_target(ctrl, class_name="UnknownClass")
        assert v.verdict is False
        assert v.reason == "default_reject_paste_capable_class"

    def test_empty_class_falls_through_to_hard_default_reject(self):
        # Empty ClassName outside the browser process list: there is no
        # control identity to soft-paste into, so the hard default_reject
        # token still applies. This is the path that Phase 1's docstring
        # step 11 describes.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="",
                     has_text_pattern=False, has_value_pattern=False)
        v = is_text_target(ctrl, class_name="",
                           process_name="myapp.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"


# --- TestConfigFactory -----------------------------------------------------


class TestConfigFactory:
    def test_build_with_no_config_uses_defaults(self):
        predicate = build_predicate_from_config({})
        # Edit + TextPattern accepts (default behaviour).
        ctrl = _ctrl()
        assert predicate.evaluate(ctrl).verdict is True
        # MenuItem rejects (default denylist).
        ctrl_menu = _ctrl(control_type=auto.ControlType.MenuItemControl,
                          control_type_name="MenuItemControl",
                          has_text_pattern=False)
        assert predicate.evaluate(ctrl_menu).verdict is False

    def test_extend_deny_class_names(self):
        config = {
            "ui": {
                "text_target": {
                    "deny_class_names_extend": ["MyAppMenu"],
                }
            }
        }
        predicate = build_predicate_from_config(config)
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="MyAppMenu", has_text_pattern=True)
        v = predicate.evaluate(ctrl, class_name="MyAppMenu")
        assert v.verdict is False
        assert v.reason == "denylist_class_name"

    def test_extend_allow_class_names(self):
        config = {
            "ui": {
                "text_target": {
                    "allow_class_names_extend": ["MyAppEdit"],
                }
            }
        }
        predicate = build_predicate_from_config(config)
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="MyAppEdit", has_text_pattern=False)
        v = predicate.evaluate(ctrl, class_name="MyAppEdit")
        assert v.verdict is True
        assert v.reason == "class_name_allowlist"

    def test_extend_deny_control_types_by_name(self):
        config = {
            "ui": {
                "text_target": {
                    "deny_control_types_extend": ["SliderControl"],
                }
            }
        }
        predicate = build_predicate_from_config(config)
        # SliderControl is NOT on the default denylist; the config
        # extension adds it.
        ctrl = _ctrl(control_type=auto.ControlType.SliderControl,
                     control_type_name="SliderControl",
                     has_text_pattern=False)
        v = predicate.evaluate(ctrl)
        assert v.verdict is False
        assert v.reason == "denylist_control_type"

    def test_unknown_control_type_in_config_logs_and_skips(self, caplog):
        config = {
            "ui": {
                "text_target": {
                    "deny_control_types_extend": ["NotARealControl",
                                                  "ButtonControl"],
                }
            }
        }
        with caplog.at_level("WARNING"):
            predicate = build_predicate_from_config(config)
        # The warning fires for the unknown name.
        assert any("NotARealControl" in r.message for r in caplog.records)
        # The known name still extends the denylist (it was already
        # in the default but the call should not have raised).
        ctrl = _ctrl(control_type=auto.ControlType.ButtonControl,
                     control_type_name="ButtonControl",
                     has_text_pattern=True)
        assert predicate.evaluate(ctrl).verdict is False


class TestConfigMalformedTypes:
    """wh-ix1z.10: malformed config types must fail closed with a warning,
    not silently iterate per character or add accidental one-character
    entries to the lists.
    """

    def test_string_value_for_deny_control_types_extend_warns_and_ignores(self, caplog):
        config = {
            "ui_actions": {
                "text_target": {
                    "deny_control_types_extend": "MenuItemControl",
                }
            }
        }
        with caplog.at_level("WARNING"):
            predicate = build_predicate_from_config(config)
        assert any(
            "deny_control_types_extend" in r.message
            and "must be a list" in r.message
            for r in caplog.records
        )
        # Default denylist is preserved; the malformed extend is ignored.
        ctrl_menu = _ctrl(control_type=auto.ControlType.MenuItemControl,
                          control_type_name="MenuItemControl",
                          has_text_pattern=False)
        assert predicate.evaluate(ctrl_menu).verdict is False

    def test_string_value_for_deny_class_names_extend_warns_and_ignores(self, caplog):
        config = {
            "ui_actions": {
                "text_target": {
                    "deny_class_names_extend": "MyMenu",
                }
            }
        }
        with caplog.at_level("WARNING"):
            predicate = build_predicate_from_config(config)
        assert any(
            "deny_class_names_extend" in r.message for r in caplog.records
        )
        # Single-character entries from a string iteration must NOT have
        # been added; a control with class "M" still passes the deny
        # check (it would still be rejected by default_reject, but not
        # by denylist_class_name).
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="M", has_text_pattern=True)
        v = predicate.evaluate(ctrl, class_name="M")
        # The accept path runs since TextPattern is present and "M" is
        # not on the deny list.
        assert v.verdict is True

    def test_string_value_for_allow_class_names_extend_warns_and_ignores(self, caplog):
        config = {
            "ui_actions": {
                "text_target": {
                    "allow_class_names_extend": "MyEdit",
                }
            }
        }
        with caplog.at_level("WARNING"):
            predicate = build_predicate_from_config(config)
        assert any(
            "allow_class_names_extend" in r.message for r in caplog.records
        )
        # Single-character entries must NOT have been added: a control
        # with class "M" without TextPattern is still rejected.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="M", has_text_pattern=False)
        assert predicate.evaluate(ctrl, class_name="M").verdict is False

    def test_non_string_entries_inside_list_warn_and_skip(self, caplog):
        config = {
            "ui_actions": {
                "text_target": {
                    "deny_class_names_extend": ["GoodEntry", 42, None,
                                                "AnotherGood"],
                }
            }
        }
        with caplog.at_level("WARNING"):
            predicate = build_predicate_from_config(config)
        # Two warnings for the two non-string entries.
        non_string_warnings = [
            r for r in caplog.records
            if "deny_class_names_extend" in r.message
            and "non-string" in r.message
        ]
        assert len(non_string_warnings) == 2
        # Good entries land on the deny list.
        ctrl_good = _ctrl(control_type=auto.ControlType.PaneControl,
                          control_type_name="PaneControl",
                          class_name="GoodEntry", has_text_pattern=True)
        assert predicate.evaluate(
            ctrl_good, class_name="GoodEntry"
        ).verdict is False

    def test_dict_value_for_extend_warns_and_ignores(self, caplog):
        # Dict, int, etc. all get the same fail-closed treatment.
        config = {
            "ui_actions": {
                "text_target": {
                    "deny_control_types_extend": {"some": "dict"},
                }
            }
        }
        with caplog.at_level("WARNING"):
            predicate = build_predicate_from_config(config)
        assert any(
            "deny_control_types_extend" in r.message
            and "dict" in r.message
            for r in caplog.records
        )
        # Defaults preserved; the predicate still works.
        ctrl_button = _ctrl(control_type=auto.ControlType.ButtonControl,
                            control_type_name="ButtonControl",
                            has_text_pattern=True)
        assert predicate.evaluate(ctrl_button).verdict is False

    def test_config_extends_do_not_remove_defaults(self):
        # User-supplied empty extends do not erase the baseline denylist.
        config = {
            "ui": {
                "text_target": {
                    "deny_control_types_extend": [],
                    "deny_class_names_extend": [],
                }
            }
        }
        predicate = build_predicate_from_config(config)
        ctrl_menu = _ctrl(control_type=auto.ControlType.MenuItemControl,
                          control_type_name="MenuItemControl",
                          has_text_pattern=False)
        assert predicate.evaluate(ctrl_menu).verdict is False
        ctrl_class = _ctrl(control_type=auto.ControlType.PaneControl,
                           control_type_name="PaneControl",
                           class_name="MenuFlyoutSubItem",
                           has_text_pattern=False)
        assert predicate.evaluate(
            ctrl_class, class_name="MenuFlyoutSubItem"
        ).verdict is False


class TestBrowserTrapClassNameOnVerdict:
    """Regression test for wh-9weum.5.1.

    The browser-empty-ClassName trap must record an empty class_name on
    the verdict so the GUI wording helper can pick the browser-trap
    wording instead of the generic OTHER bucket. A slow-path preflight
    that passes a non-empty captured-context class_name as the
    ``class_name`` parameter must NOT leak that telemetry value into
    the verdict's ``class_name`` field on the trap path.
    """

    def test_browser_trap_verdict_class_name_is_actual_not_telemetry(self):
        from unittest.mock import MagicMock
        from ui.text_target import TextTargetPredicate

        ctrl = MagicMock()
        ctrl.ControlType = 50030  # PaneControl integer
        ctrl.ControlTypeName = "Pane"
        ctrl.ClassName = ""  # browser-trap signal: empty ClassName
        ctrl.IsKeyboardFocusable = True
        ctrl.IsEnabled = True
        ctrl.GetPattern = lambda pid: None  # no TextPattern

        predicate = TextTargetPredicate()
        verdict = predicate.evaluate(
            ctrl,
            class_name="SomeCapturedContextClass",
            process_name="brave.exe",
        )

        assert verdict.verdict is False
        assert verdict.reason == "default_reject"
        # The verdict's class_name field must be the actual control's
        # empty ClassName, NOT the captured-context telemetry hint.
        # This is what the GUI wording helper checks.
        assert verdict.class_name == ""


class TestBrowserEditControlEmptyClassNameAccepts:
    """Regression test: EditControl in a browser with empty ClassName
    must accept, not hit the browser-empty-ClassName hard reject.

    Production trace 2026-05-19 17:25:52 showed dictation of
    "testing beta" into a Brave control reported by UIA as
    ControlType=EditControl, ClassName="", process=brave.exe. The
    wh-fc1x.1 trap was designed for the page document body
    (DocumentControl / PaneControl), but it was over-applying to
    EditControl, which is a strong text-input signal regardless of
    ClassName. Brave's EditControls with empty ClassName show up in
    chrome:// or brave:// settings fields, internal browser dialogs,
    and certain web <input> shapes that the accessibility tree
    surfaces as EditControl rather than as document-body shapes.
    """

    def test_edit_control_with_text_pattern_in_brave_with_empty_class_accepts(self):
        ctrl = _ctrl(control_type=auto.ControlType.EditControl,
                     control_type_name="EditControl",
                     class_name="",
                     has_text_pattern=True)
        v = is_text_target(ctrl, class_name="", process_name="brave.exe")
        assert v.verdict is True
        assert v.reason == "text_pattern_available"
        assert v.control_type == "EditControl"
        assert v.process_name == "brave.exe"

    def test_edit_control_without_text_pattern_in_brave_with_empty_class_accepts(self):
        # Some Chromium-internal EditControls do not surface TextPattern
        # at the moment of probe; the EditControl accept branch (rule 8)
        # must catch them. IsEnabled defaults to True.
        ctrl = _ctrl(control_type=auto.ControlType.EditControl,
                     control_type_name="EditControl",
                     class_name="",
                     has_text_pattern=False,
                     has_value_pattern=False)
        v = is_text_target(ctrl, class_name="", process_name="brave.exe")
        assert v.verdict is True
        assert v.reason == "edit_control"
        assert v.control_type == "EditControl"
        assert v.process_name == "brave.exe"

    def test_document_control_in_brave_with_empty_class_still_traps(self):
        # The trap still fires for the document-body shape it was
        # designed for: empty ClassName + browser process + non-Edit
        # ControlType. This is the wh-fc1x.1 case that must keep
        # rejecting.
        ctrl = _ctrl(control_type=auto.ControlType.DocumentControl,
                     control_type_name="DocumentControl",
                     class_name="",
                     has_text_pattern=True)
        v = is_text_target(ctrl, class_name="", process_name="brave.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"
        assert v.control_type == "DocumentControl"
        assert v.process_name == "brave.exe"

    def test_pane_control_in_brave_with_empty_class_still_traps(self):
        # PaneControl is the other observed page-body shape on some
        # Chromium builds. Must keep hitting the trap.
        ctrl = _ctrl(control_type=auto.ControlType.PaneControl,
                     control_type_name="PaneControl",
                     class_name="",
                     has_text_pattern=True)
        v = is_text_target(ctrl, class_name="", process_name="brave.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"

    @pytest.mark.parametrize(
        "browser", sorted(DEFAULT_BROWSER_PROCESS_NAMES)
    )
    def test_edit_control_with_text_pattern_accepts_across_browsers(self, browser):
        # Reviewer findings wh-browser-edit-empty-class-review.2 and
        # wh-browser-edit-codex-review.2: the EditControl exemption is
        # browser-process-agnostic. Parametrize from
        # DEFAULT_BROWSER_PROCESS_NAMES (the constant the predicate
        # actually consults) so a future change that adds or removes a
        # browser from the set automatically updates the test surface.
        ctrl = _ctrl(control_type=auto.ControlType.EditControl,
                     control_type_name="EditControl",
                     class_name="",
                     has_text_pattern=True)
        v = is_text_target(ctrl, class_name="", process_name=browser)
        assert v.verdict is True
        assert v.reason == "text_pattern_available"
        assert v.process_name == browser

    @pytest.mark.parametrize(
        "browser", sorted(DEFAULT_BROWSER_PROCESS_NAMES)
    )
    def test_document_control_in_browser_with_empty_class_still_traps_across_browsers(self, browser):
        # The trap must keep firing for the page document body shape on
        # every browser the predicate recognises. Parametrized from the
        # same constant as the accept test above so the trap-still-fires
        # contract follows the accept contract one for one.
        ctrl = _ctrl(control_type=auto.ControlType.DocumentControl,
                     control_type_name="DocumentControl",
                     class_name="",
                     has_text_pattern=True)
        v = is_text_target(ctrl, class_name="", process_name=browser)
        assert v.verdict is False
        assert v.reason == "default_reject"
        assert v.process_name == browser

    def test_disabled_edit_control_in_browser_with_empty_class_falls_through_to_default_reject(self):
        # Reviewer findings wh-browser-edit-empty-class-review.3 and
        # wh-browser-edit-codex-review.1: the EditControl exemption
        # bypasses the trap but does not by itself guarantee
        # acceptance. With IsEnabled=False, the EditControl accept
        # branch (rule 8) is gated off and the empty ClassName means
        # the soft-allow accept (rule 10) and soft-reject (rule 11)
        # branches are skipped. The shape lands on the final
        # default_reject at rule 12. Downstream, the rejection
        # categorizer at shared/rejection_category.py buckets
        # default_reject + browser + empty class as
        # CATEGORY_BROWSER_TRAP, and the RejectedInsertionStrategy
        # silences the rejection notice for that category. The
        # observed behavior for the user is "no keystrokes, no notice"
        # -- the same as before the EditControl exemption was added.
        #
        # Built via _ctrl with an explicit is_enabled=False so the
        # disabled-EditControl path is tested through the same fixture
        # shape as the enabled-EditControl path. A bare MagicMock would
        # silently pass even if the predicate's IsEnabled access changed
        # because MagicMock auto-creates truthy attributes on access.
        ctrl = _ctrl(control_type=auto.ControlType.EditControl,
                     control_type_name="EditControl",
                     class_name="",
                     has_text_pattern=False,
                     has_value_pattern=False,
                     is_enabled=False)
        v = is_text_target(ctrl, class_name="", process_name="brave.exe")
        assert v.verdict is False
        assert v.reason == "default_reject"
        assert v.control_type == "EditControl"
        assert v.process_name == "brave.exe"
