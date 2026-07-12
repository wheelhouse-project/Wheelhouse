"""Categorize a rejection so callers can decide what UI to show (wh-1r2b3).

The Input process needs to know the category of a rejection BEFORE
sending the rejection event to the GUI. Categories are:

  * uncertain               -- the rejected control might be a text
                               field. The GUI shows the rejection
                               notice with the Try-it-anyway button.
  * browser_trap            -- the focused control is the browser page
                               body. Cannot accept text.
  * definitely_not_text     -- the focused control is a button, menu
                               item, list item, hyperlink, or other
                               non-text control type.
  * other                   -- catch-all for stale_com, not_focusable,
                               no_focused_control, and any future
                               reason without a positive category.

Only the uncertain category should produce a rejection notice in
production. The other three are dropped silently: the user has no
useful action on them, the rejection notice without a Try-it-anyway
button is noise.

Before this module the categorization lived inside
``services/wheelhouse/rejection_toast_wording.py`` (a GUI-process
module) so the Input process could not consult it. Extracting the
category logic to ``services/wheelhouse/shared/`` lets both processes
call the same function. The wording strings stay in
``rejection_toast_wording.py``; this module returns only the category
tag.
"""

from __future__ import annotations

from typing import Iterable, Optional


CATEGORY_UNCERTAIN = "uncertain"
CATEGORY_BROWSER_TRAP = "browser_trap"
CATEGORY_DEFINITELY_NOT_TEXT = "definitely_not_text"
CATEGORY_OTHER = "other"


# Default browser process names that participate in the wh-zndq trap.
# Mirrors DEFAULT_BROWSER_PROCESS_NAMES in ui/text_target.py. The two
# lists live in different modules because text_target.py is owned by
# the Input process and this module is shared; keeping them as
# separate small literal sets is simpler than introducing a third
# shared constants file.
#
# The text-target check accepts a config-extended list at runtime via
# `[ui_actions.text_target].browser_process_names_extend`. Callers that
# need to match the predicate's resolved set (including config
# extensions) pass the resolved set into ``categorize_rejection`` via
# the ``browser_process_names`` keyword argument. Callers that pass
# nothing (the default) match against the built-in list below only.
DEFAULT_BROWSER_PROCESS_NAMES: frozenset[str] = frozenset({
    "brave.exe",
    "brave_beta.exe",
    "chrome.exe",
    "chromium.exe",
    "msedge.exe",
    "edge.exe",
    "firefox.exe",
})

# Back-compat alias for the original name. Callers that imported
# ``BROWSER_PROCESS_NAMES`` before the runtime-extension fix continue
# to work; the alias is the same frozenset.
BROWSER_PROCESS_NAMES = DEFAULT_BROWSER_PROCESS_NAMES


def categorize_rejection(
    reason: str,
    *,
    process_name: str,
    class_name: str,
    browser_process_names: Optional[Iterable[str]] = None,
) -> str:
    """Return the category tag for a rejection.

    Args:
        reason: The reason string from the text-target check
            (e.g. ``"default_reject"``,
            ``"default_reject_paste_capable_class"``,
            ``"denylist_control_type"``).
        process_name: The exe name of the process that owned the
            focused control. Compared case-insensitively against the
            browser-process set.
        class_name: The class name of the focused control. Empty is
            valid input and is itself a signal -- the browser-trap
            branch fires on empty class name in a browser process.
        browser_process_names: Optional override for the browser
            process set. The text-target check accepts config
            extensions via
            ``[ui_actions.text_target].browser_process_names_extend``;
            the Input process passes the predicate's resolved set
            (DEFAULT_BROWSER_PROCESS_NAMES plus any config additions)
            here so the categorizer's view stays consistent with the
            check's view. Callers that pass None get the built-in
            DEFAULT_BROWSER_PROCESS_NAMES only.

    Returns:
        One of CATEGORY_UNCERTAIN, CATEGORY_BROWSER_TRAP,
        CATEGORY_DEFINITELY_NOT_TEXT, CATEGORY_OTHER. Never raises.
    """

    process_lower = (process_name or "").lower()
    if browser_process_names is None:
        browser_set = DEFAULT_BROWSER_PROCESS_NAMES
    else:
        browser_set = frozenset(n.lower() for n in browser_process_names)
    is_browser = bool(process_lower) and process_lower in browser_set

    if reason == "default_reject" and is_browser and not class_name:
        return CATEGORY_BROWSER_TRAP

    if reason == "default_reject_paste_capable_class":
        return CATEGORY_UNCERTAIN

    if reason in ("denylist_control_type", "denylist_class_name"):
        return CATEGORY_DEFINITELY_NOT_TEXT

    return CATEGORY_OTHER


def should_show_try_anyway(category: str) -> bool:
    """Return True only for the uncertain category.

    The Try-it-anyway button is meaningful only when the control might
    accept text. For browser_trap, definitely_not_text, and other
    categories the user cannot do anything useful with the override,
    so the button (and after wh-1r2b3 the entire rejection notice) is
    hidden.
    """

    return category == CATEGORY_UNCERTAIN
