"""Branched-wording helper for the rejection advisory toast (wh-lzsbd).

When the text-target predicate hard-rejects a focused control, the GUI
shows an advisory toast. The wording branches by the rejection reason
so a non-technical user gets a sentence that makes sense for what
they had focused. This module is the single source of truth for the
mapping; the GUI widget consumes it.

Branches (per wh-9weum Phase 2 spec):

  * elevated (``elevated_process_window``, wh-elevated-target-notice):
      The focused window belongs to a higher-integrity (administrator)
      process; Windows discards WheelHouse's input. The toast explains
      the boundary and the fix: run WheelHouse itself as
      administrator, or use the physical keyboard. It must NOT advise
      de-elevating the target app -- some Windows apps only run as
      administrator (David's 2026-07-19 correction). No Try-it-anyway
      button: a retry can never succeed.

  * uncertain (``default_reject_paste_capable_class``):
      The focused control has a non-empty ClassName but no positive
      identity signal. The toast offers no specific reason -- a typing
      attempt may still work and Phase 4 will surface a Try-it-anyway
      button for these.

  * browser-trap (``default_reject`` from a browser process with empty
    ClassName, the wh-zndq case):
      The user has the browser body focused, not a text input. The
      toast tells them to click into a search box / comment field.

  * definitely-not-text (``denylist_control_type`` /
    ``denylist_class_name``):
      The control is a button, menu, list-item, or other clearly
      non-text control. The toast names what they have focused.

The category is also returned so Phase 4 can branch the visibility of
the Try-it-anyway button: shown only on uncertain rejects.
"""

from __future__ import annotations

from dataclasses import dataclass

# wh-1r2b3: the category constants and the categorization function
# live in services/wheelhouse/shared/rejection_category.py so both the
# Input process and the GUI process can call them. The wording module
# re-exports the constants so existing GUI-side imports keep working
# while delegating the actual category decision to the shared helper.
from shared.rejection_category import (
    CATEGORY_BROWSER_TRAP,
    CATEGORY_DEFINITELY_NOT_TEXT,
    CATEGORY_ELEVATED,
    CATEGORY_OTHER,
    CATEGORY_UNCERTAIN,
    categorize_rejection,
    should_show_try_anyway as _shared_should_show_try_anyway,
)


# ControlType to user-facing noun. Other denylist control types fall
# back to the generic "this kind of control" wording.
_CONTROL_TYPE_NOUNS: dict[str, str] = {
    "ButtonControl": "button",
    "Button": "button",
    "MenuItemControl": "menu",
    "MenuItem": "menu",
    "MenuControl": "menu",
    "MenuBarControl": "menu",
    "ListItemControl": "page background",
    "ListItem": "page background",
    "TreeItemControl": "page background",
    "TreeItem": "page background",
    "CheckBoxControl": "checkbox",
    "CheckBox": "checkbox",
    "RadioButtonControl": "radio button",
    "RadioButton": "radio button",
    "TabItemControl": "tab",
    "TabItem": "tab",
    "ToolBarControl": "toolbar",
    "ToolBar": "toolbar",
    "HyperlinkControl": "link",
    "Hyperlink": "link",
    "ImageControl": "image",
    "Image": "image",
    "SplitButtonControl": "button",
    "SplitButton": "button",
}


@dataclass(frozen=True)
class ToastWording:
    """Composed toast strings + category tag.

    ``category`` is the broad class of the rejection so downstream
    consumers (Phase 4 retry button visibility, telemetry rollups) can
    branch without reparsing the title or body.
    """

    title: str
    body: str
    category: str


def compose_rejection_wording(
    reason: str,
    *,
    control_type: str,
    process_name: str,
    class_name: str,
    app_friendly_name: str = "",
) -> ToastWording:
    """Pick title / body / category for a rejection event.

    Args:
        reason: The verdict reason from TextTargetVerdict.
        control_type: ControlTypeName (e.g. ``"ButtonControl"``).
        process_name: Lower-or-mixed-case exe name (e.g. ``"brave.exe"``).
        class_name: ClassName of the focused control. Empty string is
            valid and is itself a signal (browser-trap case).
        app_friendly_name: Optional human-readable app name. When
            provided, included in the body so the user knows which
            app rejected. Falls back silently when empty.

    Returns:
        ToastWording with title, body, and category. Always returns
        usable strings; never raises.
    """

    # wh-1r2b3: delegate the category decision to the shared helper.
    # The wording branching below mirrors the same category outcomes
    # so the user-facing strings stay in one place; the categorization
    # itself has one source of truth.
    category = categorize_rejection(
        reason=reason, process_name=process_name, class_name=class_name,
    )

    if category == CATEGORY_ELEVATED:
        # wh-elevated-target-notice. The fix is to elevate WheelHouse,
        # never to de-elevate the target: some Windows apps only run
        # as administrator, so "start the app without administrator"
        # would be advice the user may be unable to follow.
        app = app_friendly_name or "This app"
        return ToastWording(
            title="Wheelhouse can't type into administrator apps",
            body=(
                f"{app} is running as administrator, and Windows does "
                "not let Wheelhouse type into it. To dictate into "
                "administrator programs, close Wheelhouse and start it "
                "again with right-click, Run as administrator. Or use "
                "the physical keyboard for this app."
            ),
            category=CATEGORY_ELEVATED,
        )

    if category == CATEGORY_BROWSER_TRAP:
        return ToastWording(
            title="Wheelhouse couldn't type into your browser",
            body=(
                "You don't have a text box on the page selected. "
                "Click into a search box, comment field, or other typing "
                "area on the page first."
            ),
            category=CATEGORY_BROWSER_TRAP,
        )

    if category == CATEGORY_UNCERTAIN:
        body = (
            "Wheelhouse isn't sure it can type here. Click into the "
            "place where you want text, then try again."
        )
        if app_friendly_name:
            body = (
                f"Wheelhouse isn't sure it can type into {app_friendly_name}. "
                "Click into the place where you want text, then try again."
            )
        return ToastWording(
            title="Wheelhouse couldn't type that",
            body=body,
            category=CATEGORY_UNCERTAIN,
        )

    if category == CATEGORY_DEFINITELY_NOT_TEXT:
        noun = _CONTROL_TYPE_NOUNS.get(control_type or "", "")
        if noun:
            body = (
                f"Wheelhouse can't type into this. You have a {noun} "
                "selected. Click into a text box first, then try again."
            )
        else:
            body = (
                "Wheelhouse can't type into this kind of control. "
                "Click into a text box first, then try again."
            )
        return ToastWording(
            title="Wheelhouse couldn't type that",
            body=body,
            category=CATEGORY_DEFINITELY_NOT_TEXT,
        )

    # Anything else falls into the generic bucket. After wh-1r2b3 the
    # Input process silences this category before any event reaches
    # the GUI, but the helper stays defensive so a future caller does
    # not crash the GUI if it somehow does fire.
    return ToastWording(
        title="Wheelhouse couldn't type that",
        body=(
            "Wheelhouse couldn't find a place to type. Click into a "
            "text box, then try again."
        ),
        category=CATEGORY_OTHER,
    )


def should_show_try_anyway(category: str) -> bool:
    """Return True only for the uncertain category.

    Delegates to :func:`shared.rejection_category.should_show_try_anyway`
    so the rule has one source of truth across the Input process and
    the GUI process. Kept as a re-export for callers that import the
    helper from this module.
    """

    return _shared_should_show_try_anyway(category)


def detail_lines(
    *,
    process_name: str,
    class_name: str,
    control_type: str,
    reason: str,
    supported_patterns: tuple[str, ...] | list[str],
    app_friendly_name: str,
) -> list[str]:
    """Render the 'Show details' panel as a list of lines.

    Used by the toast widget's expanded section. Returns plain text
    lines; the widget formats them. Empty values render as ``"(empty)"``
    so a non-technical user can still read the panel.
    """

    def _show(value: str) -> str:
        return value if value else "(empty)"

    patterns = ", ".join(supported_patterns) if supported_patterns else "(none)"
    return [
        f"App: {_show(app_friendly_name)} ({_show(process_name)})",
        f"Control: {_show(control_type)}",
        f"Class: {_show(class_name)}",
        f"Reason: {_show(reason)}",
        f"Patterns: {patterns}",
    ]
