# -----------------------------------------------------------------------------
# Portions of this file are ported from CursorTouch/Operator-Use
# (https://github.com/CursorTouch/Operator-Use), used under the MIT License.
#
# Ported source: operator_use/computer/windows/tree/ -- specifically
#   service.py      (interactive-control filtering / tree-walk shape)
#   cache_utils.py  (the CacheRequest property + pattern set)
#   config.py       (INTERACTIVE_CONTROL_TYPE_NAMES set)
# at pinned commit 67b2d4fd8fe755223fb21ab9bba104f24f4bb04b.
#
# Copyright (c) 2026 CursorTouch
#
# MIT License
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# -----------------------------------------------------------------------------
"""Direct comtypes UI Automation tree walker for voice element clicking (wh-en45t).

Given one visible top-level window (an HWND or its UI Automation element)
this module builds ONE ``IUIAutomationCacheRequest`` covering every
property and pattern the v5 design lists, calls ``FindAllBuildCache`` ONCE
with ``TreeScope_Subtree``, and turns the returned
``IUIAutomationElementArray`` into a list of plain-data
``ElementMatch`` records (``ui/element_types.py``). Each record's
``control_ref`` is the cached COM element so the executor can call
``Invoke`` on it later without a second tree walk.

This is the walker ONLY. It does NOT implement browser DOM corrections
(``browser_dom_corrections.py``, wh-24e4w), the ``ElementFinder``
coordinator / ``UIAStrategy`` orchestration (wh-agd2v), the confidence
scorer (``ui/confidence_scorer.py`` -- already exists), or window-search
order. The caller chooses which top-level window to walk and supplies
the ``monitor_id`` resolution; ``score`` and ``is_eligible`` are filled
in later by the scorer, so every record this module emits has
``score=0.0`` and ``is_eligible=False``. Hook points are provided as
optional callables (``browser_correction_hook``, ``score_hook``) so
callers can apply those passes without this module importing them.

Single-threaded apartment (STA) requirement
============================================
UI Automation is a COM API that requires the single-threaded apartment.
Every COM call here runs sequentially on the one calling thread; the
WheelHouse Input process initialises its COM apartment as STA before this
module is used. Do NOT introduce a thread pool: worker threads that each
``CoInitialize`` create multiple STAs and cross-apartment marshaling of
UIA pointers deadlocks (this is the same lesson upstream's
``get_window_wise_nodes`` documents).

COM object lifetime (the dangling-pointer hazard)
=================================================
``comtypes`` releases a COM proxy as soon as the last Python reference to
it is garbage-collected. ``ElementMatch.control_ref`` holds a cached
element drawn from the ``IUIAutomationElementArray`` returned by
``FindAllBuildCache``; the cache lives ON that array. If the array, the
``IUIAutomation`` root, the ``CacheRequest``, or the top-level element
were allowed to be collected during the gap between the walk returning
and the later ``Invoke`` call, the proxies would be released and the
``Invoke`` would hit a dangling pointer. ``WalkResult`` therefore holds
strong references to all four objects together for exactly as long as the
matches it owns are live. Callers keep the ``WalkResult`` (not just its
``matches`` list) alive until the click decision has been applied or the
request has timed out.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, Callable, Optional

from ui.element_types import ElementMatch

logger = logging.getLogger(__name__)


# Stale-window error classes: a live-COM call (ElementFromHandle /
# FindAllBuildCache) against an HWND that closed between enumeration and the
# walk raises one of these. walk_owned_popups catches ONLY these so a popup
# closing mid-walk is skipped, while a non-stale exception (a programming error
# in a forwarded hook, e.g. browser_correction_hook / score_hook) propagates
# instead of being silently swallowed (wh-n29v.48.1). This mirrors the
# identically-named tuple in ui/element_finder.py used by the restricted
# window fall-back (_run_fallback); it is duplicated here rather than imported
# because element_finder imports FROM this module (importing back would be a
# cycle). `from comtypes import COMError` is the base-package import (NOT the
# generated UIAutomationClient type-library module), so it does not trigger
# comtypes type-library generation at import time; the guard falls back to
# (OSError,) on a host without comtypes, where OSError covers the test fakes.
try:
    from comtypes import COMError as _COMError  # type: ignore[import-not-found]

    _STALE_WINDOW_ERRORS: tuple[type[BaseException], ...] = (OSError, _COMError)
except Exception:  # noqa: BLE001 -- no comtypes -> OSError covers the test fakes
    _STALE_WINDOW_ERRORS = (OSError,)


# Recommended number of TRANSIENT stale-window retries for a PRIMARY
# focused-window walk (wh-overlay-walk-com-retry). Pass this as
# ``walk_window(..., transient_retries=WALK_TRANSIENT_RETRY_ATTEMPTS)`` from the
# overlay's focused-window walk. A focus change to a Chromium/Brave window can
# leave the window's UIA element momentarily virtualized or destroyed, so the
# live ElementFromHandle / FindAllBuildCache call raises UIA_E_ELEMENTNOTAVAILABLE
# (0x80040201, one of _STALE_WINDOW_ERRORS) even though the window is still the
# foreground and resolves cleanly a moment later. A tree walk is READ-ONLY and
# idempotent, so re-resolving the element and re-walking is side-effect-free and
# safe to retry -- the opposite of InvokePattern.Invoke, which fails CLOSED
# because it may have acted. The bound is small and the per-request deadline
# still applies, so a window that is genuinely gone (persistent error) costs only
# these few extra attempts before the last error is re-raised, unchanged, to the
# caller's never-raise wrapper. This is the count of RETRIES AFTER the first
# attempt; total attempts is this + 1. The retry is OPT-IN (walk_window defaults
# to transient_retries=0): the owned-popup walk and the by-name walk do NOT
# retry, because for a popup a raise means the menu closed and must be skipped
# fast, not retried (reviewer_0 finding wh-overlay-walk-com-retry.1.2).
WALK_TRANSIENT_RETRY_ATTEMPTS: int = 2


# ---------------------------------------------------------------------------
# UI Automation constants.
#
# Resolved lazily from comtypes' generated ``UIAutomationClient`` module the
# first time the walker touches live COM. Importing the gen module at module
# import time would force comtypes type-library generation as an import side
# effect (and break import on a machine where the walker is never run, e.g.
# the pure-logic unit tests). The IDs below are stable, documented Windows
# UI Automation constants; the helpers that build a real CacheRequest read
# them from the gen module so a comtypes regeneration cannot drift them.
# ---------------------------------------------------------------------------

# TreeScope_Subtree (element + all descendants). Stable UIA value.
TREE_SCOPE_SUBTREE = 7

# Interactive control-type IDs the walker keeps. Ported from upstream
# config.INTERACTIVE_CONTROL_TYPE_NAMES (commit 67b2d4f), mapped to the
# numeric UIA_*ControlTypeId constants. Static text, group containers, and
# panes are intentionally absent: they are dropped unless the query
# specified no role (see ``is_interactive_control_type``).
UIA_BUTTON = 50000
UIA_CHECKBOX = 50002
UIA_COMBOBOX = 50003
UIA_EDIT = 50004
UIA_HYPERLINK = 50005
UIA_LISTITEM = 50007
UIA_MENUITEM = 50011
UIA_RADIOBUTTON = 50013
UIA_SLIDER = 50015
UIA_SPINNER = 50016
UIA_TABITEM = 50019
UIA_TREEITEM = 50024
UIA_DATAITEM = 50029
UIA_SPLITBUTTON = 50031
UIA_HEADERITEM = 50035

# Non-interactive control-type ids the walker does NOT keep in its filter, but
# whose numeric ids the browser DOM-folding predicates need for locale-invariant
# role comparison (wh-l4h.1.12). Defined here next to the interactive ids so all
# UIA_*ControlTypeId constants live in one place. They are deliberately absent
# from INTERACTIVE_CONTROL_TYPE_IDS below (Text and Group are dropped by the
# interactive filter unless the query named no role).
UIA_TEXT = 50020
UIA_GROUP = 50026

# UIA_MenuControlTypeId. The classic Win32 #32768 popup-walker extension
# (wh-n29v.45) treats a top-level window whose UIA control type is Menu as the
# modern equivalent of a classic #32768 popup, so it is matched even when the
# window class name is not literally "#32768". Defined here with the other
# UIA_*ControlTypeId constants. It is NOT in INTERACTIVE_CONTROL_TYPE_IDS (a
# Menu container is not itself a clickable target; its MenuItem children are).
UIA_MENU = 50009

# The classic Win32 menu/popup window class. A visible top-level window with
# this class name that is OWNED by the focused window is the classic owned
# popup the design (lines 389-396) folds into the walk as an additional
# subtree.
CLASSIC_POPUP_CLASS_NAME = "#32768"

# The WinUI 3 popup host window class (wh-winui-menu-click-refused.1). A WinUI
# menu, flyout, or combo-box drop-down is drawn by its own top-level window of
# this class, OWNED by the application window, while the same items also appear
# inside the application window's own accessibility tree. Measured against a
# live Windows 11 Notepad File menu on 2026-08-14: the window reports UIA
# control type Pane (50033), not Menu, so the UIA_MENU arm above does not match
# it and the class name has to be matched directly. Walking it returned 13
# MenuItem matches, 12 of them exposing a working Invoke pattern, while the
# copies inside the application window exposed neither Invoke nor an MSAA
# default action and sat over pixels owned by this window -- which made every
# click on them refuse as click_point_obstructed.
WINUI_POPUP_CLASS_NAME = "Microsoft.UI.Content.PopupWindowSiteBridge"

# Every window class treated as an owned popup on the cheap (no COM) arm of
# ``_is_owned_popup``.
POPUP_CLASS_NAMES: frozenset[str] = frozenset(
    {CLASSIC_POPUP_CLASS_NAME, WINUI_POPUP_CLASS_NAME}
)

INTERACTIVE_CONTROL_TYPE_IDS: frozenset[int] = frozenset(
    {
        UIA_BUTTON,
        UIA_CHECKBOX,
        UIA_COMBOBOX,
        UIA_EDIT,
        UIA_HYPERLINK,
        UIA_LISTITEM,
        UIA_MENUITEM,
        UIA_RADIOBUTTON,
        UIA_SLIDER,
        UIA_SPINNER,
        UIA_TABITEM,
        UIA_TREEITEM,
        UIA_DATAITEM,
        UIA_SPLITBUTTON,
        UIA_HEADERITEM,
    }
)

# Canonical UIA control-type NAME (as emitted by ClickCommandParser._ROLE_KEYWORDS
# in speech/click_parser.py) -> locale-invariant numeric UIA control-type id.
# This is the single source of truth for the role-match comparison sites in
# ui/confidence_scorer.py (_role_matches) and ui/click_executor.py
# (_coord_eligible). The parser's _ROLE_KEYWORDS values are exactly these six
# names; query.role holds one of them (or None). Comparing ids instead of the
# localized CachedLocalizedControlType string makes role-qualified matching
# locale-invariant -- on non-English Windows the walker supplies a localized
# role string (German "Schaltflaeche" for a button) that would never equal the
# canonical English "Button", but the numeric id is the same in every locale
# (wh-l4h.1.15, follow-up to the wh-l4h.1.12 DOM-folding fix). It lives here
# beside the UIA_* id constants so both comparison sites import one definition
# with no import cycle (this module imports only ui.element_types).
NAME_TO_CONTROL_TYPE_ID: dict[str, int] = {
    "Button": UIA_BUTTON,
    "Hyperlink": UIA_HYPERLINK,
    "MenuItem": UIA_MENUITEM,
    "TabItem": UIA_TABITEM,
    "CheckBox": UIA_CHECKBOX,
    "Edit": UIA_EDIT,
}

# Source tag stamped on every record this module emits. "uia" in Phase 1;
# an OCR-based walker would emit "ocr" later (see v5 "Key types").
SOURCE_UIA = "uia"


@dataclass
class WalkResult:
    """Holds the matches AND the strong COM references that keep them alive.

    This is the lifetime-keeping structure the v5 "COM object lifetime"
    section requires. It is NOT frozen and is Input-process-local: the
    four ``_keepalive_*`` references and every ``control_ref`` inside
    ``matches`` are live COM proxies that cannot cross a process boundary.

    Callers MUST keep the ``WalkResult`` itself reachable (not just its
    ``matches`` list) until the click decision has been applied or the
    request has timed out. Dropping it lets ``comtypes`` garbage-collect
    the array/root/cache/top-level proxies, after which any cached
    element's ``Invoke`` hits a released pointer.
    """

    matches: list[ElementMatch]
    # (a) the IUIAutomation root pointer used to build the request.
    _keepalive_automation: Any = field(repr=False, default=None)
    # (b) the IUIAutomationCacheRequest used by the walk -- the array's
    #     cached values were materialised through it.
    _keepalive_cache_request: Any = field(repr=False, default=None)
    # (c) the IUIAutomationElementArray returned by FindAllBuildCache.
    #     The cache lives on this array; every control_ref is one element
    #     drawn from it, so this single reference keeps every match's
    #     cached COM element alive without per-match bookkeeping.
    _keepalive_element_array: Any = field(repr=False, default=None)
    # (d) the top-level element the subtree was walked from.
    _keepalive_top_level_element: Any = field(repr=False, default=None)
    # True when the per-request deadline cut this walk short -- either the
    # pre-walk skip (FindAllBuildCache never ran, matches==[]) OR the
    # per-element loop short-circuited with a PARTIAL set (wh-9f3t.54.2
    # FINDING 2). ``matches`` is then a PREFIX of the real subtree, NOT the
    # whole set, so the caller MUST NOT feed it to clear_winner_rule.decide:
    # a partial prefix can yield a confident-but-WRONG "ok" winner because the
    # real best match was never built. ElementFinder fails closed on this flag
    # and treats the walk as not_found. A normal (untruncated) walk leaves this
    # False and the matches are complete.
    deadline_truncated: bool = False

    def is_alive(self) -> bool:
        """True while all four required strong references are still held.

        The lifetime unit test calls ``gc.collect()`` between walk return
        and the simulated click and asserts this stays True -- proof that
        no required COM object was released during the gap.
        """
        return (
            self._keepalive_automation is not None
            and self._keepalive_cache_request is not None
            and self._keepalive_element_array is not None
            and self._keepalive_top_level_element is not None
        )


def is_interactive_control_type(
    control_type_id: int, *, query_has_role: bool
) -> bool:
    """Decide whether a control of this type survives the interactive filter.

    Ported from upstream's INTERACTIVE_CONTROL_TYPE_NAMES gate
    (commit 67b2d4f). When the voice query named a role
    (``query_has_role=True``) we keep only the interactive control types
    (Button, Hyperlink, MenuItem, TabItem, CheckBox, RadioButton, Edit,
    ComboBox, ListItem, TreeItem, toolbar/data/header items, ...) and drop
    static text, group containers, and panes.

    When the query specified NO role (``query_has_role=False``) the design
    keeps static text too, so the caller can match against label-only
    surfaces; this returns True for every control type in that case.
    """
    if not query_has_role:
        return True
    return control_type_id in INTERACTIVE_CONTROL_TYPE_IDS


def _rect_to_bounds(rect: Any) -> tuple[int, int, int, int]:
    """Convert a UIA BoundingRectangle to ElementMatch bounds (x, y, w, h).

    UIA exposes the cached bounding rectangle either as a ``RECT``-style
    object with ``left/top/right/bottom`` attributes (comtypes raw) or as
    a 4-tuple ``(left, top, right, bottom)`` (some bindings). Both are
    converted to the ``(x, y, width, height)`` screen-coordinate form
    ``ElementMatch.bounds`` documents. A zero rect on any failure.
    """
    try:
        if rect is None:
            return (0, 0, 0, 0)
        if isinstance(rect, (tuple, list)):
            left, top, right, bottom = rect[0], rect[1], rect[2], rect[3]
        else:
            left = rect.left
            top = rect.top
            right = rect.right
            bottom = rect.bottom
        width = int(right) - int(left)
        height = int(bottom) - int(top)
        return (int(left), int(top), width, height)
    except Exception:  # noqa: BLE001 -- any malformed rect -> zero bounds
        return (0, 0, 0, 0)


def element_match_from_cached(
    cached_element: Any,
    *,
    display_number: int,
    monitor_id: int = 0,
    source: str = SOURCE_UIA,
    item_id: Optional[str] = None,
    control_type_id: Optional[int] = None,
    source_window_hwnd: int = 0,
    source_window_is_shell: bool = False,
) -> ElementMatch:
    """Build one plain-data ``ElementMatch`` from a cached COM element.

    Reads ONLY cached properties (no live COM round-trip): the element
    came from ``FindAllBuildCache`` so name, control type, bounding
    rectangle, is-enabled, and the Invoke pattern are already materialised
    on it. ``control_ref`` is set to the cached element itself so the
    executor can ``Invoke`` it later (its lifetime is guaranteed by the
    owning ``WalkResult``'s strong reference to the element array).

    ``score`` is 0.0 and ``is_eligible`` is False by contract: the
    confidence scorer (a separate, already-existing module) fills those
    in. ``item_id`` defaults to ``"<source>-<display_number>"`` -- a
    stable per-match id assigned at walk time.

    ``control_type_id`` is the numeric UIA control-type id stamped on the
    record (the locale-invariant role signal the browser DOM-folding
    predicates compare). The build loop already reads the cached control
    type to run the interactive filter, so it threads that same value in
    here to avoid a redundant second cached read. When the caller does not
    supply it (e.g. a direct unit-test call), it is read from the cached
    control type once via ``_cached_control_type``.

    ``source_window_hwnd`` is stamped on the record (wh-n29v.45): 0 for a
    match from the primary focused-window subtree, the owning window's HWND
    for a match from an owned ``#32768`` / UIA-Menu popup subtree or a
    taskbar shell-window subtree. ``source_window_is_shell``
    (wh-overlay-taskbar-numbers) marks the taskbar case so the pre-click
    probe verifies shell-window visibility instead of popup ownership.
    """
    name = _cached_name(cached_element)
    role = _cached_localized_control_type(cached_element)
    bounds = _rect_to_bounds(_cached_bounding_rectangle(cached_element))
    is_enabled = _cached_is_enabled(cached_element)
    invoke_supported = _cached_invoke_supported(cached_element)
    resolved_control_type_id = (
        control_type_id
        if control_type_id is not None
        else _cached_control_type(cached_element)
    )
    resolved_item_id = item_id if item_id is not None else f"{source}-{display_number}"

    return ElementMatch(
        item_id=resolved_item_id,
        display_number=display_number,
        name=name,
        role=role,
        bounds=bounds,
        monitor_id=monitor_id,
        score=0.0,
        is_eligible=False,
        source=source,
        invoke_supported=invoke_supported,
        is_enabled=is_enabled,
        control_ref=cached_element,
        control_type_id=resolved_control_type_id,
        source_window_hwnd=source_window_hwnd,
        source_window_is_shell=source_window_is_shell,
    )


# ---------------------------------------------------------------------------
# Cached-property readers.
#
# Each reads exactly one cached property from a COM element and fails soft
# to a benign default. They are split out so the unit tests can drive them
# with a fake element that exposes the same cached-property surface
# (CachedName, CachedControlType, CachedLocalizedControlType,
# CachedBoundingRectangle, CachedIsEnabled, GetCachedPattern). comtypes'
# generated IUIAutomationElement exposes these as CurrentXxx for live reads
# and CachedXxx for cached reads; the walker uses the Cached* surface
# because every element came back through FindAllBuildCache.
# ---------------------------------------------------------------------------

def _cached_name(element: Any) -> str:
    """Return the element's name, falling back to the cached Legacy name.

    Prefers the UIA Name property. When that is empty, reads the cached
    LegacyIAccessible pattern's name (the CacheRequest cached the Legacy
    pattern as a fallback name source) so a control with an empty UIA
    Name but a usable Legacy name is still nameable -- and therefore
    clickable by voice. Both reads stay on the cached surface; no live
    COM round-trip is issued.
    """
    try:
        value = element.CurrentName if _prefer_current(element) else element.CachedName
        name = (value or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    if name:
        return name
    return _cached_legacy_name(element)


def _cached_legacy_name(element: Any) -> str:
    """Read the cached LegacyIAccessible pattern's name, or "" on any failure.

    Cached-surface only: ``GetCachedPattern`` for real elements,
    ``GetCurrentPattern`` only for a test fake exposing the Current*
    surface (``_prefer_current`` True). The Legacy pattern exposes the
    accessible name via its ``CachedName`` (or ``CurrentName`` on the
    Current-surface fake -- the live comtypes
    ``IUIAutomationLegacyIAccessiblePattern`` names the live property
    ``CurrentName``, not ``Name``); a non-empty value is returned stripped.
    """
    legacy_pattern_id = _uia_const("UIA_LegacyIAccessiblePatternId", 10018)
    try:
        if _prefer_current(element):
            getter = getattr(element, "GetCurrentPattern", None)
            attr = "CurrentName"
        else:
            getter = getattr(element, "GetCachedPattern", None)
            attr = "CachedName"
        if getter is None:
            return ""
        pattern = getter(legacy_pattern_id)
        if pattern is None:
            return ""
        value = getattr(pattern, attr, None)
        return (value or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _cached_localized_control_type(element: Any) -> str:
    try:
        value = (
            element.CurrentLocalizedControlType
            if _prefer_current(element)
            else element.CachedLocalizedControlType
        )
        return (value or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _cached_control_type(element: Any) -> int:
    try:
        value = (
            element.CurrentControlType
            if _prefer_current(element)
            else element.CachedControlType
        )
        return int(value)
    except Exception:  # noqa: BLE001
        return 0


def _cached_bounding_rectangle(element: Any) -> Any:
    try:
        return (
            element.CurrentBoundingRectangle
            if _prefer_current(element)
            else element.CachedBoundingRectangle
        )
    except Exception:  # noqa: BLE001
        return None


def _cached_is_enabled(element: Any) -> bool:
    try:
        value = (
            element.CurrentIsEnabled
            if _prefer_current(element)
            else element.CachedIsEnabled
        )
        return bool(value)
    except Exception:  # noqa: BLE001
        return False


def _cached_is_offscreen(element: Any) -> bool:
    """True when UIA reports the cached element off-screen (not visible).

    Reads only the cached ``IsOffscreen`` property (materialised by the walk's
    CacheRequest, which adds ``UIA_IsOffscreenPropertyId``) so there is no live
    COM round-trip. A control is off-screen when it is scrolled out of view, on
    a hidden tab, or not yet drawn -- common in a Chromium/Brave virtualized UI
    Automation tree. Any read failure returns False (treat as on-screen), which
    keeps the control: the conservative choice, since the off-screen skip is an
    optimisation, not a correctness gate.
    """
    try:
        value = (
            element.CurrentIsOffscreen
            if _prefer_current(element)
            else element.CachedIsOffscreen
        )
        return bool(value)
    except Exception:  # noqa: BLE001
        return False


def _cached_invoke_supported(element: Any) -> bool:
    """True when the cached element exposes the Invoke pattern.

    Reads ONLY the cached Invoke pattern (materialised by the walk's
    CacheRequest). UIA returns a falsy result from ``GetCachedPattern`` for
    every control that does not support Invoke -- either Python ``None`` or
    a NULL ``POINTER(IUnknown)`` (falsy, but ``is not None`` True) -- so a
    falsy cached result is the authoritative "no Invoke" answer. This
    returns False on it (via the ``bool()`` guard below) and does NOT fall
    through to a live getter. Falling through would issue a synchronous
    cross-apartment COM read for every non-invokable element (static text,
    panes, labels) inside the match-building loop, defeating the
    single-CacheRequest design on the latency-sensitive voice path.

    The live ``GetCurrentPattern`` path is reachable only for a test fake
    that models the Current* surface (``_prefer_current(element)`` True);
    a real cached element never takes it.
    """
    invoke_pattern_id = _uia_const("UIA_InvokePatternId", 10000)
    try:
        if _prefer_current(element):
            getter = getattr(element, "GetCurrentPattern", None)
            if getter is None:
                return False
            return bool(getter(invoke_pattern_id))
        getter = getattr(element, "GetCachedPattern", None)
        if getter is None:
            return False
        # Truthiness, not ``is not None``: comtypes returns a NULL
        # POINTER(IUnknown) (falsy, but ``is not None`` True) for an
        # unsupported pattern. ``is not None`` would stamp invoke_supported
        # True for a control that cannot actually be pressed, inflating the
        # scorer's Invoke bonus and breaking the clear-winner invariant. This
        # matches the press-path guard in ``_typed_invoke_pattern``
        # (reviewer_1/codex finding wh-click-invoke-on-element-not-pattern.2.1).
        return bool(getter(invoke_pattern_id))
    except Exception:  # noqa: BLE001
        return False


def _prefer_current(element: Any) -> bool:
    """Test-only escape hatch: read Current* attrs when a fake opts in.

    Reads a single explicit, non-COM marker attribute
    (``_wh_prefer_current_surface``) with a default of ``False``. No UI
    Automation property uses that name, so the ``getattr`` here can NEVER
    trigger a live cross-apartment COM read -- unlike the earlier
    ``hasattr(element, "CurrentName")`` probe, which fired a live
    ``CurrentName`` read on every real comtypes element (the wh-9f3t.6.1
    bug). Production cached elements never carry the marker, so this
    returns ``False`` without touching any COM property; the
    ``element.CurrentXxx if _prefer_current(element) else element.CachedXxx``
    operand in each reader short-circuits and the Current* branch is never
    evaluated on the production path.

    The Current-surface branch in each ``_cached_*`` reader (and in
    ``_cached_legacy_name``) is therefore test-only: it is reachable only
    when a fake explicitly sets ``_wh_prefer_current_surface = True`` to
    exercise the Current* surface. A real cached element always takes the
    Cached* path.
    """
    return getattr(element, "_wh_prefer_current_surface", False) is True


# ---------------------------------------------------------------------------
# Live-COM entry points (require an STA-initialised thread + comtypes gen).
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _uia_module() -> Any:
    """Return comtypes' generated UIAutomationClient module.

    Imported lazily so pure-logic tests need neither comtypes type-library
    generation nor a Windows desktop. On a machine where the module has
    never been generated (a fresh venv: CI, or an end user's first "click"
    before anything else touched UI Automation), importing the
    ``uiautomation`` package is NOT enough -- its COM client is a lazy
    singleton created on first API use, so the import has no generation
    side effect. Generate explicitly instead; the fallback runs once per
    venv ever, and the happy path stays a cached import
    (wh-uia-gen-fresh-venv).

    Memoized because the walk calls this once per element, not once per
    walk: ``element_match_from_cached`` reads the Invoke pattern through
    ``_cached_invoke_supported`` -> ``_uia_const`` -> here, and an element
    with no UIA Name pays a second time through ``_cached_legacy_name``.
    A window with several hundred controls therefore re-ran the import
    machinery several hundred times, and the numbered overlay re-walks on
    every focus change (wh-lru-cache-hot-paths).

    The memo belongs HERE and not on ``_uia_const``. This function raises
    when comtypes cannot resolve or generate the module, and ``lru_cache``
    does not store exceptions, so a transient generation failure is retried
    on the next call. ``_uia_const`` swallows that failure and returns its
    numeric default -- memoizing that would pin the fallback value for the
    rest of the process lifetime even after generation started working.

    Tests that monkeypatch the comtypes import surface must call
    ``_uia_module.cache_clear()`` first, or they are handed whatever a
    previous test cached (see the autouse fixture in tests/test_uia_walker.py).
    """
    try:
        from comtypes.gen import UIAutomationClient as uia_mod
    except ImportError:
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as uia_mod

    return uia_mod


def _uia_const(name: str, default: int) -> int:
    """Read a UIA constant from the gen module, falling back to ``default``.

    The numeric defaults match the documented Windows UI Automation values
    and let the pattern/property helpers work even before the gen module
    is generated (e.g. inside a unit test driving the readers with fakes).
    """
    try:
        return int(getattr(_uia_module(), name))
    except Exception:  # noqa: BLE001
        return default


class InvokePatternUnavailable(RuntimeError):
    """Raised when a control exposes no UIA Invoke pattern.

    The clear-winner rule only selects controls whose cached Invoke
    pattern was present, so this should not occur on the happy path.
    Raising (rather than silently returning) lets ``ClickExecutor`` map
    it to an execution failure instead of a no-op that would look like a
    successful click (wh-click-invoke-on-element-not-pattern).
    """


class DoDefaultActionUnavailable(RuntimeError):
    """The MSAA LegacyIAccessible / DoDefaultAction press path is unavailable.

    Raised by ``do_default_action_via_legacy_pattern`` (wh-l4h.1.17 /
    wh-click-dda-wiring) when the control exposes no resolvable MSAA
    ``LegacyIAccessiblePattern`` -- or that pattern cannot be QueryInterface'd
    to a callable ``DoDefaultAction``. This is the DoDefaultAction analogue of
    ``InvokePatternUnavailable``: a structural "there is no press path here"
    signal, NOT a COM HRESULT failure. ``ClickExecutor`` maps it to the
    ``dda_unavailable`` reason tag and fails closed -- it is not proof the
    control was pressed, so it never coordinate-clicks.

    Defined here (not in ``click_executor``) alongside the press function that
    raises it and ``InvokePatternUnavailable``; ``click_executor`` re-exports it
    so existing importers keep resolving ``ClickExecutor``'s name for it.
    """


class NoDefaultAction(RuntimeError):
    """The control exposes a Legacy pattern but has no default action to fire.

    Raised by ``do_default_action_via_legacy_pattern`` when the MSAA
    ``LegacyIAccessiblePattern`` is present but its ``DefaultAction`` is empty
    (``accDoDefaultAction`` would have nothing to perform). Distinct from
    ``DoDefaultActionUnavailable`` (no pattern at all) so the log and notice
    reflect the real cause. ``ClickExecutor`` maps it to
    ``dda_no_default_action`` and fails closed: nothing fired, so no coordinate
    click. Re-exported from ``click_executor`` for backward compatibility.
    """


class DefaultActionIsExpandCollapse(RuntimeError):
    """The control's MSAA default action is Expand or Collapse.

    Raised by ``do_default_action_via_legacy_pattern``
    (wh-treeitem-dda-wrong-action) BEFORE ``DoDefaultAction()`` is called, so
    nothing has been sent. Firing that default action toggles a tree node
    (File Explorer's navigation pane: This PC, Desktop, the drives), while a
    mouse click on the same node navigates. ``ClickExecutor`` maps it to the
    guarded coordinate click under ``dda_expand_collapse``. ``default_action``
    carries the text the control reported, for the log.
    """

    def __init__(self, default_action: str) -> None:
        super().__init__(
            f"LegacyIAccessible default action {default_action!r} is "
            "Expand or Collapse; not pressed"
        )
        self.default_action = default_action


def _invoke_pattern_class() -> Any:
    """Return the IUIAutomationInvokePattern interface class, or None.

    Needed to ``QueryInterface`` a raw cached pattern pointer into the
    typed Invoke pattern. Resolving it needs the generated comtypes
    module; on the production path that always succeeds. If it cannot be
    resolved the caller degrades to ``InvokePatternUnavailable`` (a safe
    execution-failure, not a crash or a silent no-op).
    """
    try:
        return _uia_module().IUIAutomationInvokePattern
    except Exception:  # noqa: BLE001 -- gen module absent / unresolvable
        return None


def _typed_invoke_pattern(element: Any, getter_name: str, pattern_id: int) -> Any:
    """Fetch a raw pattern pointer via ``getter_name`` and QueryInterface it
    to the typed Invoke pattern, or return None when no usable pattern exists.

    Returns None when: the getter is absent (a fake without it), the getter
    raises (an uncached pattern raises rather than returning null), the raw
    result is falsy (Python ``None`` OR a NULL ``POINTER(IUnknown)`` -- comtypes
    returns the latter for an unsupported pattern, and it is ``is not None``
    True but boolean-False), the raw result exposes no ``QueryInterface``, or
    the interface class cannot be resolved. ``QueryInterface`` is the only way
    to a callable ``Invoke``: a raw ``IUIAutomationElement`` /
    ``POINTER(IUnknown)`` has none, and the ``*As`` getters return a raw int
    (wh-click-invoke-on-element-not-pattern).
    """
    getter = getattr(element, getter_name, None)
    if getter is None:
        return None
    try:
        raw = getter(pattern_id)
    except Exception:  # noqa: BLE001 -- uncached pattern raises; treat as absent
        return None
    if not raw:  # Python None OR a NULL POINTER(IUnknown) (both falsy)
        return None
    query_interface = getattr(raw, "QueryInterface", None)
    if query_interface is None:
        return None
    iface = _invoke_pattern_class()
    if iface is None:
        return None
    try:
        return query_interface(iface)
    except Exception:  # noqa: BLE001 -- QueryInterface can raise COMError
        # Treat a present-but-unqueryable cached pointer as absent, the same
        # as every other failure path here, so invoke_via_invoke_pattern can
        # still fall back to the live current pattern instead of propagating
        # the exception and skipping the fallback. Keeping every path returning
        # None also keeps this in step with _cached_invoke_supported's
        # truthiness check (reviewer_2/deepseek finding
        # wh-click-invoke-on-element-not-pattern.3.1).
        return None


def invoke_via_invoke_pattern(element: Any) -> None:
    """Press a control through its UIA Invoke pattern.

    A real ``IUIAutomationElement`` has no ``Invoke`` method -- Invoke
    lives on ``IUIAutomationInvokePattern``, reached by fetching the raw
    pattern pointer (``GetCachedPattern`` / ``GetCurrentPattern``, which
    return ``POINTER(IUnknown)``) and ``QueryInterface``-ing it to the
    typed pattern. The walk caches that pattern via the CacheRequest, so
    fetch the CACHED pattern first (no live COM round-trip on the
    latency-sensitive voice path) and fall back to the live current
    pattern only when the cache is empty -- which the clear-winner rule
    makes unreachable on the happy path, so the fallback costs no latency
    in practice. Raise ``InvokePatternUnavailable`` when neither yields a
    pattern.

    This replaces the executor's original ``element.Invoke()`` call --
    and the first, also-broken fix that used ``GetCachedPatternAs(id, iid)``
    (that getter returns a raw int, so ``.Invoke()`` on it raised
    ``AttributeError("'int' object has no attribute 'Invoke'")``, and the
    ``iid=None`` fallback risked a native null-deref). Both were confirmed
    live against Notepad's Cancel button
    (wh-click-invoke-on-element-not-pattern).
    """
    invoke_pattern_id = _uia_const("UIA_InvokePatternId", 10000)

    pattern = _typed_invoke_pattern(element, "GetCachedPattern", invoke_pattern_id)
    if pattern is None:
        pattern = _typed_invoke_pattern(
            element, "GetCurrentPattern", invoke_pattern_id
        )
    if pattern is None:
        raise InvokePatternUnavailable("control exposes no UIA Invoke pattern")

    pattern.Invoke()


def _legacy_pattern_class() -> Any:
    """Return the IUIAutomationLegacyIAccessiblePattern interface class, or None.

    The DoDefaultAction analogue of ``_invoke_pattern_class``: needed to
    ``QueryInterface`` a raw pattern pointer into the typed Legacy pattern (the
    only surface exposing a callable ``DoDefaultAction`` / ``CurrentDefaultAction``).
    Resolving it needs the generated comtypes module; on the production path
    that always succeeds. If it cannot be resolved the caller degrades to
    ``DoDefaultActionUnavailable`` (a safe execution-failure, not a crash).
    """
    try:
        return _uia_module().IUIAutomationLegacyIAccessiblePattern
    except Exception:  # noqa: BLE001 -- gen module absent / unresolvable
        return None


def _typed_legacy_pattern(element: Any, getter_name: str, pattern_id: int) -> Any:
    """Fetch a raw pattern pointer via ``getter_name`` and QueryInterface it to
    the typed LegacyIAccessible pattern, or return None when none exists.

    The DoDefaultAction analogue of ``_typed_invoke_pattern`` with identical
    absence handling: every failure path (missing getter, getter raises, falsy
    raw incl. a NULL ``POINTER(IUnknown)``, missing ``QueryInterface``,
    unresolvable interface class, ``QueryInterface`` raising) returns None so
    the caller degrades to ``DoDefaultActionUnavailable`` rather than crashing
    or silently no-op'ing. ``QueryInterface`` is the only way to a callable
    ``DoDefaultAction``: a raw ``IUIAutomationElement`` / ``POINTER(IUnknown)``
    has none.
    """
    getter = getattr(element, getter_name, None)
    if getter is None:
        return None
    try:
        raw = getter(pattern_id)
    except Exception:  # noqa: BLE001 -- uncached pattern raises; treat as absent
        return None
    if not raw:  # Python None OR a NULL POINTER(IUnknown) (both falsy)
        return None
    query_interface = getattr(raw, "QueryInterface", None)
    if query_interface is None:
        return None
    iface = _legacy_pattern_class()
    if iface is None:
        return None
    try:
        return query_interface(iface)
    except Exception:  # noqa: BLE001 -- QueryInterface can raise COMError
        return None


# ---------------------------------------------------------------------------
# Expand / Collapse default-action detection (wh-treeitem-dda-wrong-action).
# ---------------------------------------------------------------------------

# The English verbs are always in the set, whatever the resource strings say,
# so an English-language control is detected even when every load fails.
_EXPAND_COLLAPSE_ENGLISH_FLOOR = frozenset({"expand", "collapse"})

# crewcut: these string-table IDs are undocumented. They are the resource
# strings Windows itself reports as the Expand / Collapse default action:
# oleaccrc.dll 305 / 306 (the MSAA proxies) and UIAutomationCore.dll 204 / 205
# (the UIA-to-MSAA bridge). They were measured 2026-09-14 on Windows 11
# 10.0.26200 with only the en-US language files installed, so the IDs in other
# languages are unverified, and a future Windows build could renumber them.
# A wrong ID has two harms, because the loader adds whatever string it reads
# without checking it:
#   1. A missing or empty string leaves only the English floor, so a localized
#      Expand / Collapse verb is not detected and is fired as before this fix.
#   2. A moved ID can load a genuine press verb. The neighbouring IDs measured
#      on the same machine hold oleaccrc.dll 307 "Jump", 308 "Press" and
#      UIAutomationCore.dll 203 "Switch", 206 "Check", 208 "Execute",
#      209 "Open". A control with that default action would then not be
#      pressed through DoDefaultAction. It would take the coordinate click
#      instead, which is still fully verified: the coordinate eligibility
#      check, the full re-verification of the target, and both click-point
#      checks. A match that fails the eligibility check is refused.
# To verify on a non-English Windows, or on a new Windows build: install that
# language pack, run LoadStringW over these four IDs (the same LoadLibraryExW
# flags as _resource_string_api), and compare the text with the default action
# Explorer's navigation-pane tree items report through LegacyIAccessible
# (Accessibility Insights or inspect.exe shows it).
_EXPAND_COLLAPSE_RESOURCE_IDS: tuple[tuple[str, int], ...] = (
    ("oleaccrc.dll", 305),  # Expand
    ("oleaccrc.dll", 306),  # Collapse
    ("UIAutomationCore.dll", 204),  # Expand
    ("UIAutomationCore.dll", 205),  # Collapse
)

# LoadLibraryExW flags: map the file for its resources only (no code runs, no
# DllMain), and search only System32, where both DLLs live, so a same-named
# file in the working directory cannot supply the strings.
_LOAD_LIBRARY_AS_DATAFILE = 0x00000002
_LOAD_LIBRARY_AS_IMAGE_RESOURCE = 0x00000020
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
_RESOURCE_LOAD_FLAGS = (
    _LOAD_LIBRARY_AS_DATAFILE
    | _LOAD_LIBRARY_AS_IMAGE_RESOURCE
    | _LOAD_LIBRARY_SEARCH_SYSTEM32
)

# Longest resource string read, in UTF-16 code units. A verb longer than this
# is truncated by LoadStringW and cannot match a default action, which is the
# same outcome as a failed load.
_RESOURCE_STRING_MAX_CHARS = 256


def _resource_string_api() -> Any:
    """Return an object exposing ``LoadLibraryExW``, ``LoadStringW`` and
    ``FreeLibrary`` with 64-bit-correct ctypes signatures.

    Private ``WinDLL`` instances, so setting ``argtypes`` here cannot change
    the signatures other modules gave ``ctypes.windll``. Raises on a host
    without these DLLs; ``_load_string_resource`` catches that. It is a
    separate function so tests can force each failure mode.
    """
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    load_library_ex = kernel32.LoadLibraryExW
    load_library_ex.argtypes = (wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD)
    load_library_ex.restype = wintypes.HMODULE

    free_library = kernel32.FreeLibrary
    free_library.argtypes = (wintypes.HMODULE,)
    free_library.restype = wintypes.BOOL

    load_string = user32.LoadStringW
    load_string.argtypes = (
        wintypes.HINSTANCE,
        wintypes.UINT,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    load_string.restype = ctypes.c_int

    return SimpleNamespace(
        LoadLibraryExW=load_library_ex,
        LoadStringW=load_string,
        FreeLibrary=free_library,
    )


def _load_string_resource(dll_name: str, string_id: int) -> Optional[str]:
    """Read one string-table entry from a System32 DLL, or return None.

    Never raises: a missing DLL, a missing resource (``LoadStringW`` returns
    0), an ``OSError``, or any other failure is logged and returns None, so
    the caller falls back to the English floor.
    """
    import ctypes

    try:
        api = _resource_string_api()
        module = api.LoadLibraryExW(dll_name, None, _RESOURCE_LOAD_FLAGS)
        if not module:
            logger.warning(
                "Expand/Collapse verb load: LoadLibraryExW(%s) failed "
                "(error %d); using the English verbs only",
                dll_name,
                ctypes.get_last_error(),
            )
            return None
        try:
            buffer = ctypes.create_unicode_buffer(_RESOURCE_STRING_MAX_CHARS)
            length = api.LoadStringW(
                module, string_id, buffer, _RESOURCE_STRING_MAX_CHARS
            )
        finally:
            api.FreeLibrary(module)
    except Exception as exc:  # noqa: BLE001 -- the loader must never raise
        logger.warning(
            "Expand/Collapse verb load: %s string %d failed (%r); using the "
            "English verbs only",
            dll_name,
            string_id,
            exc,
        )
        return None
    if length <= 0:
        logger.warning(
            "Expand/Collapse verb load: %s has no string %d; using the "
            "English verbs only",
            dll_name,
            string_id,
        )
        return None
    return buffer.value


@lru_cache(maxsize=1)
def _expand_collapse_verbs() -> frozenset[str]:
    """Return the casefolded default-action texts that mean Expand or Collapse.

    The strings Windows itself reports (``_EXPAND_COLLAPSE_RESOURCE_IDS``,
    read in the user's display language) plus the English floor. Memoized: the
    strings do not change while the process runs, and the first DoDefaultAction
    fallback pays the four loads once. A failed load is memoized too, as the
    floor; it is a local file read, so a retry would fail the same way. Tests
    replace ``_load_string_resource`` and call ``cache_clear()``.
    """
    verbs = set(_EXPAND_COLLAPSE_ENGLISH_FLOOR)
    for dll_name, string_id in _EXPAND_COLLAPSE_RESOURCE_IDS:
        folded = (_load_string_resource(dll_name, string_id) or "").strip().casefold()
        if folded:
            verbs.add(folded)
    return frozenset(verbs)


def do_default_action_via_legacy_pattern(element: Any) -> None:
    """Press a control through its MSAA LegacyIAccessible default action.

    The DoDefaultAction fallback for a control that exposes no UIA Invoke
    pattern (wh-l4h.1.17 / wh-click-dda-wiring). Like
    ``invoke_via_invoke_pattern``, a real ``IUIAutomationElement`` has no
    ``DoDefaultAction`` method -- it lives on
    ``IUIAutomationLegacyIAccessiblePattern``, reached by fetching the raw
    pattern pointer and ``QueryInterface``-ing it to the typed pattern.

    Three deliberate differences from the Invoke press:

    * It reads the LIVE current pattern (``GetCurrentPattern``), not the cached
      one. This is the COLD fallback -- reached only when InvokePattern is
      structurally unavailable, which the clear-winner rule makes rare -- so the
      extra live round-trip costs no hot-path latency. And the
      LegacyIAccessible ``DefaultAction`` property is NOT in the walk
      ``CacheRequest`` (only the pattern itself is cached), so a cached
      pattern's ``CachedDefaultAction`` would raise.
    * It short-circuits on an EMPTY ``DefaultAction`` with ``NoDefaultAction``
      BEFORE calling ``DoDefaultAction()``. ``accDoDefaultAction`` on a control
      with no default action can return a no-op success (``S_FALSE``), which
      comtypes does not raise on -- so a blind press would return normally and
      the executor would misread it as a real press. Reading ``DefaultAction``
      first and failing closed when it is empty prevents that false success.
    * It raises ``DefaultActionIsExpandCollapse`` BEFORE ``DoDefaultAction()``
      when the default action means Expand or Collapse
      (wh-treeitem-dda-wrong-action): the casefolded text is compared with
      ``_expand_collapse_verbs()``, the strings Windows itself uses in the
      display language plus English. Firing it would toggle a tree node where
      a click navigates, so the executor takes its guarded coordinate click
      instead. Any other non-empty default action (Press, Open, Double Click)
      is fired unchanged.

    Raises ``DoDefaultActionUnavailable`` when no Legacy pattern resolves,
    ``NoDefaultAction`` when the pattern is present but its default action is
    empty (or unreadable -- failed closed the same way; the unreadable case
    chains the original read error as the ``NoDefaultAction`` cause and logs it,
    so a transient COM failure is distinguishable in diagnostics from a control
    that genuinely has no default action). A COM error from ``DoDefaultAction()``
    itself PROPAGATES: the executor consults its HRESULT against the
    no-side-effect allowlist, so it must not be swallowed here.
    """
    legacy_pattern_id = _uia_const("UIA_LegacyIAccessiblePatternId", 10018)

    pattern = _typed_legacy_pattern(element, "GetCurrentPattern", legacy_pattern_id)
    if pattern is None:
        raise DoDefaultActionUnavailable(
            "control exposes no MSAA LegacyIAccessible pattern"
        )

    try:
        default_action = (pattern.CurrentDefaultAction or "").strip()
    except Exception as exc:  # noqa: BLE001 -- unreadable default action -> fail closed
        # The Legacy pattern resolved (GetCurrentPattern + QueryInterface both
        # succeeded), so a DefaultAction read that then raises is genuinely
        # surprising -- a transient COM failure, a mid-flight control teardown,
        # a broken MSAA implementation. Fail closed exactly like an empty
        # default action, but chain the original error and log it so this case
        # is distinguishable in diagnostics from a control that simply has no
        # default action (both surface as dda_no_default_action otherwise).
        logger.debug(
            "do_default_action_via_legacy_pattern: CurrentDefaultAction read "
            "failed; treating as no default action: %r",
            exc,
        )
        raise NoDefaultAction(
            "LegacyIAccessible pattern exposes no default action to perform"
        ) from exc

    if not default_action:
        raise NoDefaultAction(
            "LegacyIAccessible pattern exposes no default action to perform"
        )

    # wh-treeitem-dda-wrong-action: an Expand / Collapse default action toggles
    # a tree node instead of clicking it, so it is never fired. The verb set
    # holds the localized strings Windows uses plus the English floor.
    if default_action.casefold() in _expand_collapse_verbs():
        raise DefaultActionIsExpandCollapse(default_action)

    pattern.DoDefaultAction()


def create_automation() -> Any:
    """Create a fresh ``IUIAutomation`` root via CoCreateInstance.

    Must run on an STA-initialised thread. Returned object becomes
    ``WalkResult._keepalive_automation``.
    """
    import comtypes.client

    uia_mod = _uia_module()
    automation = comtypes.client.CreateObject(
        uia_mod.CUIAutomation,
        interface=uia_mod.IUIAutomation,
    )
    # CreateObject raises on failure rather than returning None; the assert
    # is a non-None guarantee for static analysis (callers, e.g.
    # CreateTrueCondition, dereference the result with no None branch).
    assert automation is not None, "CreateObject(CUIAutomation) returned None"
    return automation


def build_cache_request(automation: Any) -> Any:
    """Build the single ``CacheRequest`` used for the whole subtree walk.

    Caches every property the v5 design lists -- name, control type,
    automation id, class name, bounding rectangle, is-enabled,
    is-offscreen -- plus the Invoke and LegacyIAccessible patterns
    (LegacyIAccessible is the fallback name source). The property/pattern
    set is ported from upstream ``cache_utils.create_tree_traversal_cache``
    (commit 67b2d4f), trimmed to what the click feature needs. The scope
    is ``TreeScope_Subtree`` so a single ``FindAllBuildCache`` returns the
    whole window's element tree with every cached value in one COM
    round-trip.
    """
    uia_mod = _uia_module()
    cache_request = automation.CreateCacheRequest()

    cache_request.AddProperty(_uia_const("UIA_NamePropertyId", 30005))
    cache_request.AddProperty(_uia_const("UIA_ControlTypePropertyId", 30003))
    cache_request.AddProperty(_uia_const("UIA_LocalizedControlTypePropertyId", 30004))
    cache_request.AddProperty(_uia_const("UIA_AutomationIdPropertyId", 30011))
    cache_request.AddProperty(_uia_const("UIA_ClassNamePropertyId", 30012))
    cache_request.AddProperty(_uia_const("UIA_BoundingRectanglePropertyId", 30001))
    cache_request.AddProperty(_uia_const("UIA_IsEnabledPropertyId", 30010))
    cache_request.AddProperty(_uia_const("UIA_IsOffscreenPropertyId", 30022))

    cache_request.AddPattern(_uia_const("UIA_InvokePatternId", 10000))
    cache_request.AddPattern(_uia_const("UIA_LegacyIAccessiblePatternId", 10018))

    cache_request.TreeScope = getattr(
        uia_mod, "TreeScope_Subtree", TREE_SCOPE_SUBTREE
    )
    return cache_request


def element_from_hwnd(automation: Any, hwnd: int) -> Any:
    """Resolve a top-level window HWND to its UI Automation element."""
    return automation.ElementFromHandle(hwnd)


def point_hits_winner(
    automation: Any,
    winner_ref: Any,
    x: int,
    y: int,
    *,
    max_hops: int = 64,
) -> bool:
    """Does the UIA element at physical screen ``(x, y)`` belong to the winner?

    The second obstruction layer of the executor's coordinate fallback
    (wh-explorer-navpane-click.1.4): the root-window comparison cannot see a
    SAME-ROOT occluder (an in-window overlay -- a Chromium in-page modal, a
    same-process floating panel -- shares the top-level window). This asks
    UI Automation which element is actually at the click point and accepts
    the click only when that element resolves to the winner itself, a
    descendant of it (a button's own text or icon child), or one of its
    containers. The container direction is accepted on purpose: a weak
    accessibility implementation legitimately answers ``ElementFromPoint``
    with a coarse container (or the window element itself), and refusing
    those would break classic Win32 apps that click fine today -- the cost
    is that same-root occluders inside such apps are not caught.

    ``CompareElements`` compares UIA runtime IDs, so the check works for
    windowless frameworks (Chromium, WinUI) where no per-control window
    handle exists. Both ancestor walks are bounded by ``max_hops``.

    Returns False when no element is at the point or no relation is found
    within the hop budget. COM errors propagate: the executor maps any raise
    from this seam to a fail-closed ``click_point_obstructed`` refusal, and
    fabricating False here would only relabel the same refusal while hiding
    the real error from the log.
    """
    uia_mod = _uia_module()
    pt = uia_mod.tagPOINT()
    pt.x = int(x)
    pt.y = int(y)
    hit = automation.ElementFromPoint(pt)
    if hit is None:
        return False
    walker = automation.ControlViewWalker
    current = hit
    for _ in range(max_hops):
        if automation.CompareElements(current, winner_ref):
            return True
        current = walker.GetParentElement(current)
        if current is None:
            break
    current = winner_ref
    for _ in range(max_hops):
        if automation.CompareElements(current, hit):
            return True
        current = walker.GetParentElement(current)
        if current is None:
            break
    return False


def walk_window(
    top_level: Any,
    *,
    automation: Optional[Any] = None,
    query_has_role: bool = True,
    monitor_id: int = 0,
    browser_correction_hook: Optional[Callable[[list[ElementMatch]], list[ElementMatch]]] = None,
    score_hook: Optional[Callable[[list[ElementMatch]], list[ElementMatch]]] = None,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    cache_request: Optional[Any] = None,
    source_window_hwnd: int = 0,
    source_window_is_shell: bool = False,
    transient_retries: int = 0,
    skip_offscreen_or_zero_area: bool = False,
) -> WalkResult:
    """Walk one top-level window and return a ``WalkResult``.

    ``top_level`` is either an HWND (int) or an already-resolved UI
    Automation element. Exactly one ``FindAllBuildCache(TreeScope_Subtree)``
    runs per call. Exactly one ``CacheRequest`` is built per call UNLESS the
    caller supplies one via ``cache_request`` -- the classic ``#32768``
    popup-walker extension (wh-n29v.45) passes the PRIMARY walk's cache request
    so every popup subtree is walked under ONE shared ``CacheRequest`` (design
    line 392). The supplied request is retained on this ``WalkResult`` too so
    its keepalive chain is self-contained.

    ``source_window_hwnd`` (wh-n29v.45) is stamped on every match this walk
    builds: 0 for the primary focused-window walk, the owning window's HWND
    for an owned-popup or taskbar shell-window subtree walk so the pre-click
    probe can later verify that window. ``source_window_is_shell``
    (wh-overlay-taskbar-numbers) is stamped alongside it: True only for a
    taskbar shell-window walk, selecting the visibility-only shell probe over
    the visible-AND-owned popup probe.

    Hook points (this slice implements NEITHER, per scope ownership):
    - ``browser_correction_hook`` -- where a caller in the Chromium-family
      case applies ``browser_dom_corrections`` (wh-24e4w). Receives and
      returns the match list.
    - ``score_hook`` -- where the caller applies the confidence scorer
      (``ui/confidence_scorer.py``) to fill in ``score``/``is_eligible``.
      Receives and returns the match list.

    Both hooks default to ``None`` (identity). The returned ``WalkResult``
    holds the strong COM references; keep it alive until the click is done.

    A non-None ``browser_correction_hook`` is also this function's signal that
    the walk is a browser walk: only then does the build loop mark a row whose
    rectangle is not fully inside the Menu element before it
    (``bounds_outside_menu``, wh-vscode-menu-badge-misplaced). The hook, not
    ``query_has_role``, is the signal, because a role-less by-name query over
    a native window also walks with ``query_has_role=False``. Native walks
    leave every match at the default, False.

    Deadline bound (wh-9f3t.54.2)
    =============================
    ``deadline`` is a SINGLE ABSOLUTE monotonic timestamp (the same clock
    ``clock`` reads), NOT a per-call duration. The caller (ElementFinder, fed
    by UIActionHandler) anchors ONE deadline per click REQUEST at command-
    dequeue time and passes that same value into the focused-window walk AND
    every fall-back-window walk, so the total block across all walks is bounded
    by the one deadline -- not (1+N) * a per-call budget (FINDING 1). Charging
    the deadline from command-dequeue (not walk entry) also folds the pre-walk
    SharedMemory/foreground-capture/ElementFromHandle latency into the budget,
    so the walk gives up before the Logic awaiter's IPC-send-anchored timeout
    rather than after it (FINDING 3).

    The single ``FindAllBuildCache`` COM call is NOT interruptible mid-call, so
    two levers apply:

    * **Pre-walk bound.** If the deadline has ALREADY passed AT ENTRY, the walk
      is skipped BEFORE any blocking COM call -- including ``ElementFromHandle``
      for an int HWND, which is itself a live UIA round-trip that can block or
      fail against a hung / elevated / cross-process window (FINDING B,
      wh-9f3t.73.2). An empty, ``deadline_truncated=True`` ``WalkResult`` is
      returned (0 matches), having touched no COM.
    * **Post-call deadline check.** AFTER the array comes back the per-element
      scoring/build loop short-circuits once the clock passes the deadline.
      The returned matches are then a PREFIX of the subtree and the
      ``WalkResult`` is flagged ``deadline_truncated=True`` so the caller
      FAILS CLOSED (treats it as not_found) rather than running clear-winner
      over a partial set and possibly returning a wrong "ok" winner (FINDING 2).

    ``deadline=None`` (the default) disables both bounds -- the walk processes
    the whole subtree unconditionally, preserving the pre-wh-9f3t.54.2 behaviour
    for every caller that does not opt in. ``clock`` is injectable so tests
    drive the deadline deterministically.
    """
    own_automation = automation is None
    if own_automation:
        automation = create_automation()
    # create_automation never returns None (it raises on failure); assert so
    # type-checkers narrow `automation` for the COM calls below. own_automation
    # is kept separately for cleanup-ownership bookkeeping.
    assert automation is not None

    # PRE-WALK BOUND, CHECKED FIRST (FINDING B, wh-9f3t.73.2): if the per-request
    # deadline has already passed at entry, return BEFORE resolving an int HWND
    # via ElementFromHandle. Production focused/fall-back calls pass HWND ints,
    # and ElementFromHandle is a live UIA COM call that can block or fail against
    # a hung / elevated / cross-process window. Checking the deadline ahead of it
    # means an already-expired budget touches no COM at all. No top-level element
    # was resolved, so the keepalive is None; matches is empty either way.
    if deadline is not None and clock() >= deadline:
        logger.debug(
            "uia_walker: per-request deadline exhausted at entry; skipping the "
            "walk before ElementFromHandle (0 elements processed)"
        )
        return WalkResult(
            matches=[],
            _keepalive_automation=automation,
            _keepalive_cache_request=None,
            _keepalive_element_array=None,
            _keepalive_top_level_element=None,
            deadline_truncated=True,
        )

    # Build the shared CacheRequest / true-condition / tree-scope ONCE before
    # the walk loop. They depend only on ``automation`` (not on the resolved
    # element), never go stale, and are reused across any retry. The popup
    # walker may pass a CacheRequest to share (so the focused window and every
    # owned popup use ONE shared request, design line 392); respect it. The
    # shared request is still retained on this WalkResult's keepalive chain so
    # the result is self-contained. Building these ahead of the element
    # resolution lets the retry loop below re-resolve a stale top-level element
    # without rebuilding them.
    if cache_request is None:
        cache_request = build_cache_request(automation)
    uia_mod = _uia_module()
    true_condition = automation.CreateTrueCondition()
    tree_scope = getattr(uia_mod, "TreeScope_Subtree", TREE_SCOPE_SUBTREE)

    # THE single subtree walk, with an OPT-IN bounded retry on a TRANSIENT
    # stale-window error (``transient_retries``; 0 = no retry, the default for
    # the by-name walk and every owned-popup walk). Each attempt re-resolves the
    # top-level element -- an int HWND via a fresh ElementFromHandle (so a stale
    # handle from a prior attempt is replaced), an already-resolved element
    # reused as-is -- then runs FindAllBuildCache, returning one
    # IUIAutomationElementArray with every element's cached values from one COM
    # round-trip. BOTH the element resolution AND the FindAllBuildCache run
    # INSIDE the try, so a stale-window error from ElementFromHandle is retried
    # too (reviewer_0 finding wh-overlay-walk-com-retry.1.1): the production
    # overlay walk passes an int HWND, so ElementFromHandle runs live every
    # attempt and is exactly where a virtualized browser window can raise. On a
    # stale-window error the walk is retried (re-resolving the element) until the
    # bound is reached, at which point the last error is re-raised UNCHANGED so
    # the caller's never-raise wrapper fails the walk exactly as it did before
    # the retry existed. A NON-stale exception (a programming error, e.g. a bad
    # forwarded hook) is NOT caught and propagates immediately, matching
    # walk_owned_popups (wh-n29v.48.1). Before each RETRY (the first attempt was
    # already deadline-checked at entry) the per-request deadline is re-checked
    # so retries never overrun the budget; an exhausted deadline returns an empty
    # deadline_truncated WalkResult rather than burning the remaining attempts.
    #
    # The retry is OPT-IN per call on purpose. A tree walk is read-only and
    # idempotent, so re-walking the FOCUSED window is safe and the overlay opts
    # in. An owned-popup walk does NOT opt in: there a raise means the menu
    # CLOSED between enumeration and the walk, so retrying would waste the shared
    # deadline on a gone window and could starve later popups (reviewer_0 finding
    # wh-overlay-walk-com-retry.1.2). The by-name walk keeps its prior behaviour
    # too (default 0).
    element_array = None
    top_level_element = None
    for attempt in range(transient_retries + 1):
        if attempt > 0 and deadline is not None and clock() >= deadline:
            logger.debug(
                "uia_walker: per-request deadline exhausted before stale-window "
                "retry attempt %d/%d; abandoning the walk (0 elements "
                "processed)",
                attempt + 1, transient_retries + 1,
            )
            return WalkResult(
                matches=[],
                _keepalive_automation=automation,
                _keepalive_cache_request=cache_request,
                _keepalive_element_array=None,
                _keepalive_top_level_element=None,
                deadline_truncated=True,
            )
        try:
            if isinstance(top_level, int):
                top_level_element = element_from_hwnd(automation, top_level)
            else:
                top_level_element = top_level
            element_array = top_level_element.FindAllBuildCache(
                tree_scope, true_condition, cache_request
            )
            break
        except _STALE_WINDOW_ERRORS as exc:
            if attempt >= transient_retries:
                logger.debug(
                    "uia_walker: stale-window error on the final walk attempt "
                    "(%d/%d); re-raising: %r",
                    attempt + 1, transient_retries + 1, exc,
                )
                raise
            logger.debug(
                "uia_walker: transient stale-window error on walk attempt "
                "%d/%d; re-resolving the element and retrying: %r",
                attempt + 1, transient_retries + 1, exc,
            )

    # The loop's last iteration always either breaks (success) or re-raises
    # (exhausted), and an exhausted deadline returns early -- so reaching here
    # means a walk succeeded and the array is set. Assert it to narrow the type
    # for the build below and to document the invariant.
    assert element_array is not None

    matches, truncated = _build_matches_from_array(
        element_array,
        query_has_role=query_has_role,
        monitor_id=monitor_id,
        deadline=deadline,
        clock=clock,
        source_window_hwnd=source_window_hwnd,
        source_window_is_shell=source_window_is_shell,
        skip_offscreen_or_zero_area=skip_offscreen_or_zero_area,
        mark_bounds_outside_menu=browser_correction_hook is not None,
    )

    # FAIL-CLOSED ON A TRUNCATED PARTIAL SET (FINDING 2): the hooks
    # (browser corrections + scorer) and clear-winner MUST NOT run over a
    # prefix of the subtree -- a partial set can produce a confident-but-wrong
    # winner. When the build loop short-circuited, return the flagged
    # WalkResult WITHOUT applying the hooks; the caller maps it to not_found.
    if truncated:
        return WalkResult(
            matches=[],
            _keepalive_automation=automation,
            _keepalive_cache_request=cache_request,
            _keepalive_element_array=element_array,
            _keepalive_top_level_element=top_level_element,
            deadline_truncated=True,
        )

    if browser_correction_hook is not None:
        matches = browser_correction_hook(matches)
    if score_hook is not None:
        matches = score_hook(matches)

    return WalkResult(
        matches=matches,
        _keepalive_automation=automation,
        _keepalive_cache_request=cache_request,
        _keepalive_element_array=element_array,
        _keepalive_top_level_element=top_level_element,
        deadline_truncated=False,
    )


def _build_matches_from_array(
    element_array: Any,
    *,
    query_has_role: bool,
    monitor_id: int,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    source_window_hwnd: int = 0,
    source_window_is_shell: bool = False,
    skip_offscreen_or_zero_area: bool = False,
    mark_bounds_outside_menu: bool = False,
) -> tuple[list[ElementMatch], bool]:
    """Iterate a cached element array, filter, and build ElementMatch records.

    Returns ``(matches, truncated)``. ``truncated`` is True when the
    per-element loop short-circuited on the deadline before processing every
    element -- the matches are then a PREFIX of the subtree, not the whole set.

    Shared by the live walk and by tests (which pass a fake array exposing
    ``Length`` and ``GetElement(i)``). Filtering reads the cached
    ControlType; ``display_number`` is 1-based and assigned in iteration
    order over the kept elements.

    ``skip_offscreen_or_zero_area`` (opt-in, off by default) additionally
    drops any control UIA reports off-screen or whose cached rectangle has
    zero width or height, so a dropped control does NOT consume a
    ``display_number``. Only the overlay walk opts in
    (wh-overlay-stale-click-refresh); the by-name find and owned-popup walks
    keep every interactive control.

    ``mark_bounds_outside_menu`` (opt-in, off by default;
    wh-vscode-menu-badge-misplaced) sets ``bounds_outside_menu=True`` on a
    kept match whose rectangle is not fully inside the rectangle of the
    nearest Menu element (UIA 50009) BEFORE it in this array. The array is
    in pre-order, so that Menu is the menu the row belongs to or an earlier
    one; a nested submenu that comes later replaces it. Every element in the
    array counts as a Menu candidate, including one the filters drop. A
    match with no Menu before it is never marked. A rectangle that cannot
    be read converts to ``(0, 0, 0, 0)``, which a real row is never fully
    inside, so a read failure marks the row: the mark only makes the overlay
    keep its normal badge placement, never the reverse. ``walk_window``
    turns it on for a browser walk only.

    POST-CALL DEADLINE CHECK (wh-9f3t.54.2): when ``deadline`` is not None this
    loop is the interruptible part of the walk. Before processing each element
    it checks ``clock() >= deadline`` and, if passed, STOPS and reports
    ``truncated=True``. The check is at the top of the loop body so progress is
    bounded by the deadline regardless of subtree size. ``deadline=None``
    processes the whole array (``truncated`` always False). The caller fails
    closed on a truncated result rather than scoring a partial set.
    """
    matches: list[ElementMatch] = []
    length = _array_length(element_array)
    display_number = 0
    # Bounds of the nearest Menu element seen so far (None before the first).
    menu_bounds: Optional[tuple[int, int, int, int]] = None
    for index in range(length):
        if deadline is not None and clock() >= deadline:
            logger.debug(
                "uia_walker: per-request deadline passed mid-walk; "
                "short-circuiting per-element loop after %d of %d elements "
                "(%d partial matches discarded, failing closed)",
                index,
                length,
                len(matches),
            )
            return matches, True
        element = _array_get(element_array, index)
        if element is None:
            continue
        control_type_id = _cached_control_type(element)
        # The menu this element is checked against is the one BEFORE it, so
        # read it before a Menu element replaces it with its own bounds.
        preceding_menu_bounds = menu_bounds
        if mark_bounds_outside_menu and control_type_id == UIA_MENU:
            menu_bounds = _rect_to_bounds(_cached_bounding_rectangle(element))
        if not is_interactive_control_type(
            control_type_id, query_has_role=query_has_role
        ):
            continue
        # wh-overlay-stale-click-refresh: the overlay opts into this so it never
        # paints a badge on a control the user cannot click. Off-screen controls
        # (scrolled out, hidden tab) and zero-area controls have no usable badge
        # position and are refused at pre-click verification (bounds_invalid)
        # anyway, so numbering them only wastes a number and confuses the user.
        # Both reads are cached (no live COM round-trip).
        if skip_offscreen_or_zero_area:
            if _cached_is_offscreen(element):
                continue
            _, _, width, height = _rect_to_bounds(
                _cached_bounding_rectangle(element)
            )
            if width <= 0 or height <= 0:
                continue
        display_number += 1
        match = element_match_from_cached(
            element,
            display_number=display_number,
            monitor_id=monitor_id,
            control_type_id=control_type_id,
            source_window_hwnd=source_window_hwnd,
            source_window_is_shell=source_window_is_shell,
        )
        if preceding_menu_bounds is not None and not _bounds_inside(
            match.bounds, preceding_menu_bounds
        ):
            match = replace(match, bounds_outside_menu=True)
        matches.append(match)
    return matches, False


def _bounds_inside(
    inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]
) -> bool:
    """True when ``(x, y, w, h)`` rectangle ``inner`` lies fully inside
    ``outer`` (shared edges count as inside)."""
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return ix >= ox and iy >= oy and ix + iw <= ox + ow and iy + ih <= oy + oh


def _array_length(element_array: Any) -> int:
    try:
        return int(element_array.Length)
    except Exception:  # noqa: BLE001
        try:
            return len(element_array)
        except Exception:  # noqa: BLE001
            return 0


def _array_get(element_array: Any, index: int) -> Any:
    try:
        return element_array.GetElement(index)
    except Exception:  # noqa: BLE001
        try:
            return element_array[index]
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Classic Win32 #32768 owned-popup walker extension (wh-n29v.45).
#
# design-v4.md "Classic Win32 `#32768` popup-walker extension" (lines 389-396):
# in addition to the focused window's subtree, walk every VISIBLE top-level
# window that is OWNED by the focused window and whose class name is "#32768"
# (the UIA Menu control type is the modern equivalent and is also matched) as
# an ADDITIONAL subtree under ONE shared CacheRequest.
#
# Detection (``enumerate_owned_popups``) and the per-popup subtree walk
# (``walk_owned_popups``) live HERE; ElementFinder orchestrates the primary +
# popup walks, the split deadline, and the merge order. Every Win32 / COM seam
# is an injected callable so the headless test suite drives fakes (mirroring
# the rest of this module and ElementFinder).
# ---------------------------------------------------------------------------


def _default_enumerate_top_level_windows() -> list[int]:
    """Real-Win32 default enumerator of top-level window HWNDs.

    Lazily imports ``win32gui`` so this module stays importable on a headless
    test host. Returns an empty list on any failure (fail soft -> no popups).
    """
    try:
        import win32gui  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 -- no win32gui on a headless host
        return []
    hwnds: list[int] = []

    def _collect(hwnd: int, _extra: Any) -> bool:
        hwnds.append(int(hwnd))
        return True

    try:
        win32gui.EnumWindows(_collect, None)
    except Exception:  # noqa: BLE001 -- enumeration can race a closing window
        return hwnds
    return hwnds


def make_caching_enumerator(
    enumerator: Optional[Callable[[], list[int]]] = None,
) -> Callable[[], list[int]]:
    """One-call cache around a top-level-window enumerator.

    ``overlay_walk`` hands ONE of these to both the owned-popup walk and the
    taskbar walk so a single ``EnumWindows`` pass serves both additional
    subtree walks instead of each paying its own blocking Win32 enumeration
    (deepseek finding wh-overlay-taskbar-numbers.5.1). The underlying
    enumerator runs lazily on the FIRST call -- a walk the budget gate skips
    never triggers it -- and an empty result is cached like any other (an
    empty desktop enumeration is a valid answer, not a miss).
    """
    resolved = (
        _default_enumerate_top_level_windows if enumerator is None else enumerator
    )
    cached: Optional[list[int]] = None

    def _cached_enumerator() -> list[int]:
        nonlocal cached
        if cached is None:
            cached = resolved()
        return cached

    return _cached_enumerator


def _default_owner_of(hwnd: int) -> int:
    """Real-Win32 default: the owner HWND of a top-level window (GWL_HWNDPARENT).

    For an owned popup ``GetWindow(hwnd, GW_OWNER)`` returns the owner. Returns
    0 on any failure so an unreadable window cannot masquerade as owned.
    """
    try:
        import win32con  # type: ignore[import-not-found]
        import win32gui  # type: ignore[import-not-found]

        return int(win32gui.GetWindow(hwnd, win32con.GW_OWNER))
    except Exception:  # noqa: BLE001
        return 0


def _default_class_name_of(hwnd: int) -> str:
    try:
        import win32gui  # type: ignore[import-not-found]

        return str(win32gui.GetClassName(hwnd))
    except Exception:  # noqa: BLE001
        return ""


def _default_is_window_visible(hwnd: int) -> bool:
    try:
        import win32gui  # type: ignore[import-not-found]

        return bool(win32gui.IsWindowVisible(hwnd))
    except Exception:  # noqa: BLE001
        return False


def _make_default_control_type_of(automation: Any) -> Callable[[int], int]:
    """Build the default UIA control-type lookup over the given automation root.

    Reads ``ElementFromHandle(hwnd).CurrentControlType`` -- a LIVE COM read, so
    it is consulted only when the cheaper class-name check did not already match
    (see ``_is_owned_popup``). Returns 0 on any failure so an unreadable window
    cannot match on control type.
    """

    def _control_type_of(hwnd: int) -> int:
        try:
            element = automation.ElementFromHandle(hwnd)
            return int(element.CurrentControlType)
        except Exception:  # noqa: BLE001
            return 0

    return _control_type_of


def _is_owned_popup(
    hwnd: int,
    focused_hwnd: int,
    *,
    owner_fn: Callable[[int], int],
    class_name_fn: Callable[[int], str],
    visible_fn: Callable[[int], bool],
    control_type_fn: Callable[[int], int],
) -> bool:
    """True when ``hwnd`` is a visible top-level popup owned by the focused window.

    The check (design line 391, extended by wh-winui-menu-click-refused.1):
    owner == focused window AND (class name is in ``POPUP_CLASS_NAMES`` OR UIA
    control type == Menu) AND visible. The control-type check is the modern
    equivalent and is consulted ONLY when the cheap class-name check did not
    match, so the live COM read it costs is avoided for every window class named
    in ``POPUP_CLASS_NAMES``. The focused window is never its own popup.

    ``POPUP_CLASS_NAMES`` holds two classes: the classic Win32 ``#32768`` menu
    window, and the WinUI 3 popup host that draws menus in modern Windows 11
    applications. The WinUI host needs its own name here because it reports UIA
    control type Pane, so the Menu arm never matches it.

    Any seam raising (a window closed between enumeration and the probe) makes
    this return False -- a missing window is not a popup.
    """
    if hwnd == focused_hwnd:
        return False
    try:
        if owner_fn(hwnd) != focused_hwnd:
            return False
        if not visible_fn(hwnd):
            return False
        if class_name_fn(hwnd) in POPUP_CLASS_NAMES:
            return True
        # Modern equivalent: a UIA Menu control-type top-level window.
        return control_type_fn(hwnd) == UIA_MENU
    except Exception:  # noqa: BLE001 -- window raced closed -> not a popup
        return False


def enumerate_owned_popups(
    focused_hwnd: int,
    *,
    enumerator: Callable[[], list[int]] = _default_enumerate_top_level_windows,
    owner_fn: Callable[[int], int] = _default_owner_of,
    class_name_fn: Callable[[int], str] = _default_class_name_of,
    visible_fn: Callable[[int], bool] = _default_is_window_visible,
    control_type_fn: Callable[[int], int],
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[int]:
    """Return the HWNDs of visible owned popup windows (wh-n29v.45).

    A popup is a classic ``#32768`` window, a WinUI 3 popup host window, or any
    other owned top-level window whose UIA control type is Menu.

    Walks the injected top-level-window enumerator and keeps every HWND that
    satisfies ``_is_owned_popup`` against ``focused_hwnd``. Preserves the
    enumerator's order (Win32's z-order). A per-window seam failure skips that
    window only (a popup that raced closed), never aborts the scan.

    ``control_type_fn`` has no module-level default because the production value
    is bound to a live ``IUIAutomation`` root (see
    ``_make_default_control_type_of``); ElementFinder supplies it. Tests inject
    a fake.

    Deadline (wh-n29v.47.2): ``control_type_fn`` is the only EXPENSIVE seam here
    -- it is a live UIA ``ElementFromHandle`` COM round trip, consulted for every
    visible owned window whose class is not in ``POPUP_CLASS_NAMES``. The cheap
    owner/class/visible checks are unbounded, but the live-COM probe is gated:
    once ``deadline`` is spent (``clock() >= deadline``) the control-type arm is
    replaced by a no-op that returns 0 (never a popup), so no further COM probe
    fires. A window whose class is in ``POPUP_CLASS_NAMES`` still matches after
    the deadline because its match comes from the cheap class-name check, never
    the COM probe. ``deadline=None``
    (the default / no-budget path) behaves exactly as before -- the real
    ``control_type_fn`` is used unconditionally.
    """
    if deadline is not None:
        def _deadline_aware_control_type(hwnd: int) -> int:
            # Once the shared budget is spent, do NOT issue the live-COM
            # ElementFromHandle probe; report "not a Menu" so detection falls
            # back to the cheap class-name check alone (wh-n29v.47.2).
            if clock() >= deadline:
                return 0
            return control_type_fn(hwnd)

        effective_control_type_fn = _deadline_aware_control_type
    else:
        effective_control_type_fn = control_type_fn

    popups: list[int] = []
    try:
        candidates = enumerator()
    except Exception:  # noqa: BLE001 -- enumeration can fail wholesale
        return []
    for hwnd in candidates:
        if _is_owned_popup(
            hwnd,
            focused_hwnd,
            owner_fn=owner_fn,
            class_name_fn=class_name_fn,
            visible_fn=visible_fn,
            control_type_fn=effective_control_type_fn,
        ):
            popups.append(hwnd)
    return popups


def walk_owned_popups(
    focused_hwnd: int,
    *,
    automation: Any,
    cache_request: Any,
    query_has_role: bool = True,
    monitor_id: int = 0,
    browser_correction_hook: Optional[
        Callable[[list[ElementMatch]], list[ElementMatch]]
    ] = None,
    score_hook: Optional[
        Callable[[list[ElementMatch]], list[ElementMatch]]
    ] = None,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    enumerator: Callable[[], list[int]] = _default_enumerate_top_level_windows,
    owner_fn: Callable[[int], int] = _default_owner_of,
    class_name_fn: Callable[[int], str] = _default_class_name_of,
    visible_fn: Callable[[int], bool] = _default_is_window_visible,
    control_type_fn: Optional[Callable[[int], int]] = None,
) -> list[WalkResult]:
    """Walk each owned popup window as an additional subtree.

    design lines 389-396: every visible top-level window owned by
    ``focused_hwnd`` whose class is in ``POPUP_CLASS_NAMES`` (or whose UIA
    control type is Menu) is walked
    as an additional subtree using the SAME ``cache_request`` (and the SAME
    ``automation`` root) the PRIMARY walk used. Returns one ``WalkResult`` per
    popup that produced a usable (non-truncated) walk; each popup-sourced match
    carries ``source_window_hwnd`` = the owning popup HWND so the pre-click
    popup-closed probe can verify the popup is still visible + owned later.

    No owned popup present (the common case) -> returns an empty list having
    issued NO ``ElementFromHandle`` / ``FindAllBuildCache`` for a popup (no
    extra COM round-trip beyond the cheap enumeration/owner/class checks).

    Shared deadline: ``deadline`` is the SINGLE absolute monotonic timestamp the
    caller also passed to the primary walk -- it is threaded unchanged into each
    popup ``walk_window`` (with the same ``clock``) so the popup walks share the
    one budget. A popup walk the deadline cut short (``deadline_truncated``) is
    DROPPED rather than appended as a partial prefix (fail closed, matching the
    primary walk's contract). A popup whose ``ElementFromHandle`` raises (the
    popup closed between enumeration and walk) is skipped, not fatal.
    """
    # Bound the enumeration+probe phase against the shared budget (wh-n29v.47.2).
    # find() guards ENTRY via _popup_share_allows, but enumerate_owned_popups
    # then runs the (potentially per-window live-COM) detection BEFORE the first
    # per-popup walk_window's own pre-deadline guard, so that work was outside
    # the budget. If the deadline is already spent on entry, do no enumeration at
    # all and return whatever we have (nothing yet). enumerate_owned_popups also
    # receives the deadline+clock so its live-COM control_type_fn probe stops
    # firing once the budget is spent. deadline=None keeps the old behaviour.
    if deadline is not None and clock() >= deadline:
        return []

    if control_type_fn is None:
        control_type_fn = _make_default_control_type_of(automation)

    popup_hwnds = enumerate_owned_popups(
        focused_hwnd,
        enumerator=enumerator,
        owner_fn=owner_fn,
        class_name_fn=class_name_fn,
        visible_fn=visible_fn,
        control_type_fn=control_type_fn,
        deadline=deadline,
        clock=clock,
    )

    results: list[WalkResult] = []
    for popup_hwnd in popup_hwnds:
        try:
            popup_result = walk_window(
                popup_hwnd,
                automation=automation,
                query_has_role=query_has_role,
                monitor_id=monitor_id,
                browser_correction_hook=browser_correction_hook,
                score_hook=score_hook,
                deadline=deadline,
                clock=clock,
                cache_request=cache_request,
                source_window_hwnd=popup_hwnd,
            )
        except _STALE_WINDOW_ERRORS:
            # The popup closed between enumeration and the walk: the live-COM
            # ElementFromHandle / FindAllBuildCache raised OSError / COMError.
            # Skip this popup and try the next. A NON-stale exception (a
            # programming error in browser_correction_hook / score_hook, which
            # walk_window runs un-guarded) is NOT caught here -- it propagates
            # so a real bug surfaces rather than silently dropping the popup,
            # matching _run_fallback / _walk_and_decide in element_finder.py
            # (wh-n29v.48.1).
            logger.debug(
                "uia_walker: owned-popup walk of HWND %s raised a stale-window "
                "error; skipping",
                popup_hwnd,
                exc_info=True,
            )
            continue
        if popup_result.deadline_truncated:
            # The shared budget ran out (or was already spent) for this popup;
            # do NOT append a partial/empty prefix as if it were the popup's
            # full contents. Fail closed, matching the primary walk.
            continue
        results.append(popup_result)
    return results


# ---------------------------------------------------------------------------
# Taskbar shell-window walker extension (wh-overlay-taskbar-numbers).
#
# v1 scope (design doc 2026-08-04-overlay-bubble-badges-design-v1.md
# "Follow-up"): in addition to the focused window's subtree, the numbered
# overlay walks the taskbar shell windows -- the primary taskbar, the
# per-monitor secondary taskbars, and the tray-overflow flyout when it is
# open -- so taskbar buttons get numbers. The shell windows are always-on-top,
# so no occlusion filtering is needed. Detection is class-name + visibility
# only: shell windows are UNOWNED top-levels (no owner check) and the class
# check needs no live COM (no control-type probe). ElementFinder orchestrates
# the merge order; the by-name find() path never walks the taskbar.
# ---------------------------------------------------------------------------

# The closed set of top-level window classes the taskbar walk targets.
# Shell_TrayWnd is the primary taskbar (all Windows versions);
# Shell_SecondaryTrayWnd is the per-extra-monitor taskbar;
# NotifyIconOverflowWindow is the Win10 tray-overflow flyout;
# TopLevelWindowForOverflowXamlIsland is the Win11 tray-overflow flyout.
TASKBAR_WINDOW_CLASSES = frozenset({
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland",
})


def enumerate_taskbar_windows(
    *,
    exclude_hwnd: int = 0,
    enumerator: Callable[[], list[int]] = _default_enumerate_top_level_windows,
    class_name_fn: Callable[[int], str] = _default_class_name_of,
    visible_fn: Callable[[int], bool] = _default_is_window_visible,
) -> list[int]:
    """Return the HWNDs of visible taskbar shell windows, in z-order.

    Keeps every enumerated top-level HWND whose class name is in
    ``TASKBAR_WINDOW_CLASSES`` and that is visible. A hidden shell window --
    the closed tray-overflow flyout keeps its HWND while invisible -- is
    skipped. ``exclude_hwnd`` names the window the primary overlay walk
    already covers: when the taskbar itself IS the focused window, walking it
    again here would double-badge every button, so it is excluded.

    The class check runs before the visibility check because most enumerated
    windows fail it, and both seams are cheap Win32 reads (no live COM). A
    per-window seam failure (the window closed between enumeration and the
    probe) skips that window only; a wholesale enumeration failure returns an
    empty list -- the overlay then numbers the focused window alone.
    """
    taskbars: list[int] = []
    try:
        candidates = enumerator()
    except Exception:  # noqa: BLE001 -- enumeration can fail wholesale
        return []
    for hwnd in candidates:
        if hwnd == exclude_hwnd:
            continue
        try:
            if class_name_fn(hwnd) not in TASKBAR_WINDOW_CLASSES:
                continue
            if not visible_fn(hwnd):
                continue
        except Exception:  # noqa: BLE001 -- window raced closed -> skip it
            continue
        taskbars.append(hwnd)
    return taskbars


def walk_taskbar_windows(
    *,
    automation: Any,
    cache_request: Any,
    monitor_id: int = 0,
    score_hook: Optional[
        Callable[[list[ElementMatch]], list[ElementMatch]]
    ] = None,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    exclude_hwnd: int = 0,
    enumerator: Callable[[], list[int]] = _default_enumerate_top_level_windows,
    class_name_fn: Callable[[int], str] = _default_class_name_of,
    visible_fn: Callable[[int], bool] = _default_is_window_visible,
) -> list[WalkResult]:
    """Walk each visible taskbar shell window as an additional subtree.

    Mirrors :func:`walk_owned_popups`: every walk shares the caller's
    ``automation`` root and ``cache_request`` (one ``CacheRequest`` across the
    primary walk and every additional subtree) and the SINGLE absolute
    ``deadline``. Each taskbar-sourced match is stamped with
    ``source_window_hwnd`` = the shell window's HWND and
    ``source_window_is_shell=True`` so the pre-click probe verifies the shell
    window is still visible (no owner check -- shell windows are unowned)
    instead of running the popup probe.

    Three contracts are hardcoded rather than parameterised, because the
    taskbar is always explorer's, never a browser:

    * ``query_has_role=True`` -- the interactive-control filter always runs;
      there is no DOM-correction hook.
    * ``skip_offscreen_or_zero_area=True`` -- an auto-hidden taskbar reports
      ``IsWindowVisible`` True while its buttons sit off-screen; badging a
      button the user cannot see wastes a number.
    * No ``browser_correction_hook``.

    Best-effort suffix work: a deadline already spent on entry does no
    enumeration and returns ``[]``; a per-window walk the deadline cuts short
    is DROPPED (fail closed, never a partial prefix); a window whose
    ``ElementFromHandle`` raises a stale-window error (the overflow flyout
    closed between enumeration and the walk) is skipped. A NON-stale
    exception (a programming error in ``score_hook``) propagates, matching
    ``walk_owned_popups``.
    """
    if deadline is not None and clock() >= deadline:
        return []

    taskbar_hwnds = enumerate_taskbar_windows(
        exclude_hwnd=exclude_hwnd,
        enumerator=enumerator,
        class_name_fn=class_name_fn,
        visible_fn=visible_fn,
    )

    results: list[WalkResult] = []
    for taskbar_hwnd in taskbar_hwnds:
        # Pre-walk identity re-check (codex finding
        # wh-overlay-taskbar-numbers.5.3). HWNDs are recycled: between
        # enumeration and this walk the handle can be destroyed and reissued
        # to an unrelated live window, which walk_window would then badge as
        # taskbar content. Re-reading class membership and visibility
        # immediately before the walk narrows that window to microseconds
        # (the executor's click-time class re-check closes the longer
        # walk-to-click window). A re-read that raises means the window died
        # mid-probe -- drop it like any stale window.
        try:
            if class_name_fn(taskbar_hwnd) not in TASKBAR_WINDOW_CLASSES:
                continue
            if not visible_fn(taskbar_hwnd):
                continue
        except Exception:  # noqa: BLE001 -- window raced closed -> skip it
            continue
        try:
            taskbar_result = walk_window(
                taskbar_hwnd,
                automation=automation,
                query_has_role=True,
                monitor_id=monitor_id,
                score_hook=score_hook,
                deadline=deadline,
                clock=clock,
                cache_request=cache_request,
                source_window_hwnd=taskbar_hwnd,
                source_window_is_shell=True,
                skip_offscreen_or_zero_area=True,
            )
        except _STALE_WINDOW_ERRORS:
            logger.debug(
                "uia_walker: taskbar walk of HWND %s raised a stale-window "
                "error; skipping",
                taskbar_hwnd,
                exc_info=True,
            )
            continue
        if taskbar_result.deadline_truncated:
            continue
        results.append(taskbar_result)
    return results
