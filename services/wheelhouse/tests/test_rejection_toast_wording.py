"""Tests for the rejection-toast wording helper (wh-lzsbd).

The wording helper picks branched title/body strings based on the
rejection reason, control type, process name, and class name. It is
the single source of truth for the user-facing wording so the GUI
widget stays declarative and the wording can be updated with no GUI
test churn.
"""

from __future__ import annotations

import pytest

from rejection_toast_wording import (
    CATEGORY_BROWSER_TRAP,
    CATEGORY_DEFINITELY_NOT_TEXT,
    CATEGORY_OTHER,
    CATEGORY_UNCERTAIN,
    compose_rejection_wording,
    detail_lines,
)


class TestBrowserTrap:
    def test_browser_empty_classname_default_reject(self):
        wording = compose_rejection_wording(
            reason="default_reject",
            control_type="DocumentControl",
            process_name="brave.exe",
            class_name="",
        )
        assert wording.category == CATEGORY_BROWSER_TRAP
        assert wording.title == "WheelHouse couldn't type into your browser"
        assert "search box" in wording.body
        assert "comment field" in wording.body

    def test_browser_trap_case_insensitive_process(self):
        wording = compose_rejection_wording(
            reason="default_reject",
            control_type="DocumentControl",
            process_name="BRAVE.EXE",
            class_name="",
        )
        assert wording.category == CATEGORY_BROWSER_TRAP

    def test_chrome_chromium_edge_firefox_all_browser_trap(self):
        for process in ("chrome.exe", "chromium.exe", "msedge.exe", "firefox.exe", "edge.exe"):
            wording = compose_rejection_wording(
                reason="default_reject",
                control_type="DocumentControl",
                process_name=process,
                class_name="",
            )
            assert wording.category == CATEGORY_BROWSER_TRAP, (
                f"{process} did not produce browser_trap"
            )

    def test_browser_with_non_empty_classname_is_not_browser_trap(self):
        # The wh-zndq trap fires only on empty ClassName. A focused
        # text input in the same browser produces a different reason
        # entirely; defensively, even default_reject with non-empty
        # classname does NOT take the browser-trap branch.
        wording = compose_rejection_wording(
            reason="default_reject",
            control_type="DocumentControl",
            process_name="brave.exe",
            class_name="SomeBrowserChild",
        )
        assert wording.category != CATEGORY_BROWSER_TRAP

    def test_non_browser_default_reject_is_not_browser_trap(self):
        wording = compose_rejection_wording(
            reason="default_reject",
            control_type="WindowControl",
            process_name="explorer.exe",
            class_name="",
        )
        assert wording.category != CATEGORY_BROWSER_TRAP


class TestUncertain:
    def test_default_reject_paste_capable_uses_uncertain(self):
        wording = compose_rejection_wording(
            reason="default_reject_paste_capable_class",
            control_type="WindowControl",
            process_name="zed.exe",
            class_name="Zed::Window",
        )
        assert wording.category == CATEGORY_UNCERTAIN
        assert wording.title == "WheelHouse couldn't type that"
        assert "isn't sure" in wording.body

    def test_uncertain_includes_app_name_when_provided(self):
        wording = compose_rejection_wording(
            reason="default_reject_paste_capable_class",
            control_type="WindowControl",
            process_name="zed.exe",
            class_name="Zed::Window",
            app_friendly_name="Zed Editor",
        )
        assert "Zed Editor" in wording.body

    def test_uncertain_omits_app_name_when_empty(self):
        wording = compose_rejection_wording(
            reason="default_reject_paste_capable_class",
            control_type="WindowControl",
            process_name="zed.exe",
            class_name="Zed::Window",
            app_friendly_name="",
        )
        assert wording.category == CATEGORY_UNCERTAIN
        assert wording.body.startswith("WheelHouse isn't sure")


class TestDefinitelyNotText:
    @pytest.mark.parametrize(
        "control_type,expected_noun",
        [
            ("ButtonControl", "button"),
            ("MenuItemControl", "menu"),
            ("ListItemControl", "page background"),
            ("TreeItemControl", "page background"),
            ("CheckBoxControl", "checkbox"),
            ("RadioButtonControl", "radio button"),
            ("TabItemControl", "tab"),
            ("ToolBarControl", "toolbar"),
            ("HyperlinkControl", "link"),
            ("ImageControl", "image"),
        ],
    )
    def test_denylist_control_type_picks_noun(
        self, control_type: str, expected_noun: str,
    ):
        wording = compose_rejection_wording(
            reason="denylist_control_type",
            control_type=control_type,
            process_name="explorer.exe",
            class_name="",
        )
        assert wording.category == CATEGORY_DEFINITELY_NOT_TEXT
        assert expected_noun in wording.body
        assert "Click into a text box" in wording.body

    def test_denylist_class_name_uses_definitely_not_text(self):
        wording = compose_rejection_wording(
            reason="denylist_class_name",
            control_type="MenuItemControl",
            process_name="explorer.exe",
            class_name="MenuFlyoutSubItem",
        )
        assert wording.category == CATEGORY_DEFINITELY_NOT_TEXT
        assert "menu" in wording.body

    def test_unknown_control_type_uses_generic_wording(self):
        wording = compose_rejection_wording(
            reason="denylist_control_type",
            control_type="GroupControl",
            process_name="explorer.exe",
            class_name="",
        )
        assert wording.category == CATEGORY_DEFINITELY_NOT_TEXT
        assert "kind of control" in wording.body


class TestOtherFallback:
    def test_unknown_reason_uses_other_category(self):
        wording = compose_rejection_wording(
            reason="some_future_reason",
            control_type="EditControl",
            process_name="zed.exe",
            class_name="Zed::Window",
        )
        assert wording.category == CATEGORY_OTHER
        # Always returns usable strings.
        assert wording.title
        assert wording.body

    def test_stale_com_uses_other_category(self):
        wording = compose_rejection_wording(
            reason="stale_com",
            control_type="",
            process_name="zed.exe",
            class_name="",
        )
        assert wording.category == CATEGORY_OTHER


class TestDetailLines:
    def test_renders_all_fields(self):
        lines = detail_lines(
            process_name="zed.exe",
            class_name="Zed::Window",
            control_type="WindowControl",
            reason="default_reject_paste_capable_class",
            supported_patterns=("Invoke",),
            app_friendly_name="Zed Editor",
        )
        joined = "\n".join(lines)
        assert "Zed Editor" in joined
        assert "zed.exe" in joined
        assert "WindowControl" in joined
        assert "Zed::Window" in joined
        assert "default_reject_paste_capable_class" in joined
        assert "Invoke" in joined

    def test_empty_fields_render_as_empty_marker(self):
        lines = detail_lines(
            process_name="",
            class_name="",
            control_type="",
            reason="",
            supported_patterns=(),
            app_friendly_name="",
        )
        joined = "\n".join(lines)
        assert "(empty)" in joined
        assert "(none)" in joined

    def test_supported_patterns_list_or_tuple(self):
        as_list = detail_lines(
            process_name="x", class_name="y", control_type="z",
            reason="r", supported_patterns=["A", "B"], app_friendly_name="X",
        )
        as_tuple = detail_lines(
            process_name="x", class_name="y", control_type="z",
            reason="r", supported_patterns=("A", "B"), app_friendly_name="X",
        )
        assert as_list == as_tuple
