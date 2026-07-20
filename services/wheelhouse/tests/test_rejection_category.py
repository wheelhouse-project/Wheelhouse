"""Tests for the shared rejection-category function (wh-1r2b3).

The category function classifies a rejection reason + process name +
class name combination into one of four categories. The Input process
uses the category to decide whether to send the rejection event at
all; the GUI process uses it to decide what wording to show and
whether to display the Try-it-anyway button.
"""

from __future__ import annotations

from shared.rejection_category import (
    BROWSER_PROCESS_NAMES,
    CATEGORY_BROWSER_TRAP,
    CATEGORY_DEFINITELY_NOT_TEXT,
    CATEGORY_ELEVATED,
    CATEGORY_OTHER,
    CATEGORY_UNCERTAIN,
    DEFAULT_BROWSER_PROCESS_NAMES,
    categorize_rejection,
    should_emit_notice,
    should_show_try_anyway,
)


class TestCategoryUncertain:
    def test_default_reject_paste_capable_class(self):
        category = categorize_rejection(
            reason="default_reject_paste_capable_class",
            process_name="zed.exe",
            class_name="Zed::Window",
        )
        assert category == CATEGORY_UNCERTAIN

    def test_uncertain_in_browser_process(self):
        category = categorize_rejection(
            reason="default_reject_paste_capable_class",
            process_name="brave.exe",
            class_name="Chrome_RenderWidgetHostHWND",
        )
        assert category == CATEGORY_UNCERTAIN


class TestCategoryBrowserTrap:
    def test_default_reject_brave_empty_class(self):
        category = categorize_rejection(
            reason="default_reject",
            process_name="brave.exe",
            class_name="",
        )
        assert category == CATEGORY_BROWSER_TRAP

    def test_default_reject_chrome_empty_class(self):
        category = categorize_rejection(
            reason="default_reject",
            process_name="chrome.exe",
            class_name="",
        )
        assert category == CATEGORY_BROWSER_TRAP

    def test_default_reject_process_case_insensitive(self):
        category = categorize_rejection(
            reason="default_reject",
            process_name="BRAVE.EXE",
            class_name="",
        )
        assert category == CATEGORY_BROWSER_TRAP

    def test_all_browser_processes_match(self):
        for process in BROWSER_PROCESS_NAMES:
            category = categorize_rejection(
                reason="default_reject",
                process_name=process,
                class_name="",
            )
            assert category == CATEGORY_BROWSER_TRAP, (
                f"{process} should produce browser_trap"
            )

    def test_non_empty_class_breaks_browser_trap(self):
        category = categorize_rejection(
            reason="default_reject",
            process_name="brave.exe",
            class_name="SomeBrowserChild",
        )
        assert category != CATEGORY_BROWSER_TRAP

    def test_non_browser_process_breaks_browser_trap(self):
        category = categorize_rejection(
            reason="default_reject",
            process_name="explorer.exe",
            class_name="",
        )
        assert category != CATEGORY_BROWSER_TRAP


class TestCategoryDefinitelyNotText:
    def test_denylist_control_type(self):
        category = categorize_rejection(
            reason="denylist_control_type",
            process_name="notepad.exe",
            class_name="Notepad",
        )
        assert category == CATEGORY_DEFINITELY_NOT_TEXT

    def test_denylist_class_name(self):
        category = categorize_rejection(
            reason="denylist_class_name",
            process_name="explorer.exe",
            class_name="SysListView32",
        )
        assert category == CATEGORY_DEFINITELY_NOT_TEXT


class TestCategoryOther:
    def test_stale_com(self):
        category = categorize_rejection(
            reason="stale_com",
            process_name="zed.exe",
            class_name="Zed::Window",
        )
        assert category == CATEGORY_OTHER

    def test_not_focusable(self):
        category = categorize_rejection(
            reason="not_focusable",
            process_name="zed.exe",
            class_name="Zed::Window",
        )
        assert category == CATEGORY_OTHER

    def test_no_focused_control(self):
        category = categorize_rejection(
            reason="no_focused_control",
            process_name="",
            class_name="",
        )
        assert category == CATEGORY_OTHER

    def test_default_reject_non_browser_non_empty_class(self):
        # default_reject outside the browser-trap conditions falls
        # into the other bucket. This is the case that wh-1r2b3
        # silences in production.
        category = categorize_rejection(
            reason="default_reject",
            process_name="zed.exe",
            class_name="Zed::Window",
        )
        assert category == CATEGORY_OTHER

    def test_unknown_reason_string(self):
        category = categorize_rejection(
            reason="some_future_reason_we_have_not_seen_yet",
            process_name="zed.exe",
            class_name="Zed::Window",
        )
        assert category == CATEGORY_OTHER


class TestShouldShowTryAnyway:
    def test_uncertain_shows(self):
        assert should_show_try_anyway(CATEGORY_UNCERTAIN) is True

    def test_browser_trap_hides(self):
        assert should_show_try_anyway(CATEGORY_BROWSER_TRAP) is False

    def test_definitely_not_text_hides(self):
        assert should_show_try_anyway(CATEGORY_DEFINITELY_NOT_TEXT) is False

    def test_other_hides(self):
        assert should_show_try_anyway(CATEGORY_OTHER) is False

    def test_unknown_category_hides(self):
        # Defensive: an unknown category string should not show the
        # override button. Caller error should not produce false
        # positives in the UI.
        assert should_show_try_anyway("not_a_real_category") is False


class TestBrowserProcessNamesOverride:
    """wh-1r2b3.2.1: the categorizer accepts a config-extended browser set.

    The text-target check builds a resolved browser-process set at
    startup from DEFAULT_BROWSER_PROCESS_NAMES plus the entries in
    [ui_actions.text_target].browser_process_names_extend. The
    categorize_rejection function accepts that resolved set via the
    browser_process_names keyword argument so the categorizer's view
    of the browser set matches the check's view.
    """

    def test_config_extended_browser_matches(self):
        # A process not in the default set is recognized as a browser
        # when callers pass an extended set.
        category = categorize_rejection(
            reason="default_reject",
            process_name="arc.exe",
            class_name="",
            browser_process_names=DEFAULT_BROWSER_PROCESS_NAMES | {"arc.exe"},
        )
        assert category == CATEGORY_BROWSER_TRAP

    def test_extended_set_matches_case_insensitive(self):
        category = categorize_rejection(
            reason="default_reject",
            process_name="ARC.EXE",
            class_name="",
            browser_process_names={"arc.exe"},
        )
        assert category == CATEGORY_BROWSER_TRAP

    def test_extended_set_replaces_default(self):
        # When callers pass an explicit set, the default list is NOT
        # merged in. The set the caller passes is the only set used.
        category = categorize_rejection(
            reason="default_reject",
            process_name="brave.exe",
            class_name="",
            browser_process_names={"arc.exe"},
        )
        # brave.exe is no longer in the active browser set, so the
        # default_reject + empty-class hit falls through to other.
        assert category == CATEGORY_OTHER

    def test_none_uses_default_set(self):
        # Default behavior: passing None or omitting the kwarg uses
        # the built-in DEFAULT_BROWSER_PROCESS_NAMES.
        category = categorize_rejection(
            reason="default_reject",
            process_name="brave.exe",
            class_name="",
            browser_process_names=None,
        )
        assert category == CATEGORY_BROWSER_TRAP


class TestCategoryElevated:
    """wh-elevated-target-notice: the router synthesizes reason
    ``elevated_process_window`` when the focused window belongs to a
    higher-integrity process. The category must win over every other
    branch -- the reason is definitive, so process and class names
    must not re-route it."""

    def test_elevated_process_window(self):
        category = categorize_rejection(
            reason="elevated_process_window",
            process_name="regedit.exe",
            class_name="RegEdit_RegEdit",
        )
        assert category == CATEGORY_ELEVATED

    def test_elevated_wins_over_browser_trap_shape(self):
        # An elevated browser process with an empty class name must
        # still categorize as elevated, not browser_trap.
        category = categorize_rejection(
            reason="elevated_process_window",
            process_name="chrome.exe",
            class_name="",
        )
        assert category == CATEGORY_ELEVATED

    def test_elevated_with_empty_identity(self):
        # UIA visibility into elevated windows is unreliable, so the
        # synthesized verdict may carry empty process and class names.
        category = categorize_rejection(
            reason="elevated_process_window",
            process_name="",
            class_name="",
        )
        assert category == CATEGORY_ELEVATED

    def test_elevated_never_shows_try_anyway(self):
        # A retry can never succeed against an elevated window --
        # Windows discards the input again. No override button.
        assert should_show_try_anyway(CATEGORY_ELEVATED) is False


class TestShouldEmitNotice:
    """wh-elevated-target-notice: the Input-process emission gate.

    ``should_emit_notice`` decides whether the rejection event is sent
    to the GUI at all. It must be True for exactly the categories that
    produce a useful notice: uncertain (notice + Try-it-anyway) and
    elevated (notice explaining the administrator boundary, no
    button). The wh-1r2b3 silencing contract for browser_trap,
    definitely_not_text, and other must remain intact."""

    def test_uncertain_emits(self):
        assert should_emit_notice(CATEGORY_UNCERTAIN) is True

    def test_elevated_emits(self):
        assert should_emit_notice(CATEGORY_ELEVATED) is True

    def test_browser_trap_stays_silenced(self):
        assert should_emit_notice(CATEGORY_BROWSER_TRAP) is False

    def test_definitely_not_text_stays_silenced(self):
        assert should_emit_notice(CATEGORY_DEFINITELY_NOT_TEXT) is False

    def test_other_stays_silenced(self):
        assert should_emit_notice(CATEGORY_OTHER) is False

    def test_unknown_category_stays_silenced(self):
        assert should_emit_notice("not_a_real_category") is False


class TestDefensiveInputHandling:
    def test_empty_process_name_does_not_match_browser(self):
        category = categorize_rejection(
            reason="default_reject",
            process_name="",
            class_name="",
        )
        assert category == CATEGORY_OTHER

    def test_none_like_inputs_do_not_crash(self):
        # The function takes strs and should not be passed None, but
        # if a caller passes the empty string for everything the
        # function should still return a usable category.
        category = categorize_rejection(
            reason="",
            process_name="",
            class_name="",
        )
        assert category == CATEGORY_OTHER
