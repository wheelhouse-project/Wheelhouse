"""Tests for the classic Win32 #32768 owned-popup walker extension (wh-n29v.45).

design-v4.md "Classic Win32 `#32768` popup-walker extension" (lines 389-396):

* A popup is a VISIBLE top-level window whose owner == the focused window AND
  whose class name == ``#32768`` (the UIA control-type Menu is also matched).
* Each popup is walked as an additional subtree using a CacheRequest SHARED
  with the primary walk.
* Each popup-sourced ElementMatch carries the owning popup HWND so the
  pre-click probe can later verify the popup is still visible + owned.

These tests drive the pure popup-detection + popup-subtree-walk logic with the
same fake cached-element / element-array / automation surface the existing
test_uia_walker.py defines. No live COM, no real display.
"""

import gc

import pytest

from ui import uia_walker
from ui.uia_walker import (
    CLASSIC_POPUP_CLASS_NAME,
    UIA_BUTTON,
    UIA_MENU,
    UIA_MENUITEM,
    WalkResult,
    enumerate_owned_popups,
    walk_owned_popups,
)

# Reuse the established fakes.
from tests.test_uia_walker import (
    FakeAutomation,
    FakeCacheRequest,
    FakeCachedElement,
    FakeElementArray,
    FakeTopLevel,
)


# ---------------------------------------------------------------------------
# Fake Win32 + automation surface for popup enumeration / subtree walks.
# ---------------------------------------------------------------------------


class FakeWindow:
    """A visible/owned/classed top-level window, modelled for enumeration."""

    def __init__(self, hwnd, *, owner, class_name, visible=True,
                 control_type=UIA_MENU, elements=None):
        self.hwnd = hwnd
        self.owner = owner
        self.class_name = class_name
        self.visible = visible
        self.control_type = control_type
        self.elements = elements or []


class FakeDesktop:
    """Models the Win32 seams enumerate_owned_popups consumes.

    Provides an injectable enumerator of candidate top-level HWNDs plus the
    per-HWND owner / class-name / visibility / UIA-control-type lookups the
    detection predicate reads.
    """

    def __init__(self, windows):
        self._by_hwnd = {w.hwnd: w for w in windows}
        self._order = [w.hwnd for w in windows]

    def enumerate(self):
        return list(self._order)

    def owner_of(self, hwnd):
        return self._by_hwnd[hwnd].owner

    def class_name_of(self, hwnd):
        return self._by_hwnd[hwnd].class_name

    def is_visible(self, hwnd):
        return self._by_hwnd[hwnd].visible

    def control_type_of(self, hwnd):
        return self._by_hwnd[hwnd].control_type


class PopupAwareAutomation(FakeAutomation):
    """FakeAutomation that resolves a popup HWND to a fake top-level element.

    ElementFromHandle returns a FakeTopLevel over the window's own element
    array, so walk_owned_popups can drive the real walk machinery per popup.
    Counts ElementFromHandle calls so a no-popup test can prove no extra COM.
    """

    def __init__(self, desktop):
        super().__init__(element_array=None)
        self._desktop = desktop
        self.element_from_handle_calls = 0

    def ElementFromHandle(self, hwnd):
        self.element_from_handle_calls += 1
        window = self._desktop._by_hwnd[hwnd]
        return FakeTopLevel(FakeElementArray(window.elements))


FOCUSED_HWND = 1000


# ---------------------------------------------------------------------------
# enumerate_owned_popups: the detection predicate.
# ---------------------------------------------------------------------------


def test_enumerate_matches_classic_32768_owned_visible():
    desktop = FakeDesktop([
        FakeWindow(2001, owner=FOCUSED_HWND, class_name=CLASSIC_POPUP_CLASS_NAME,
                   control_type=UIA_MENU),
    ])
    popups = enumerate_owned_popups(
        FOCUSED_HWND,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=desktop.control_type_of,
    )
    assert popups == [2001]


def test_enumerate_matches_uia_menu_control_type_even_without_class():
    """design line 391: the UIA Menu control type also matches, even if the
    class name is not the classic #32768."""
    desktop = FakeDesktop([
        FakeWindow(2002, owner=FOCUSED_HWND, class_name="SomeModernPopup",
                   control_type=UIA_MENU),
    ])
    popups = enumerate_owned_popups(
        FOCUSED_HWND,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=desktop.control_type_of,
    )
    assert popups == [2002]


def test_enumerate_skips_not_owned_by_focused():
    desktop = FakeDesktop([
        FakeWindow(2003, owner=9999, class_name=CLASSIC_POPUP_CLASS_NAME),
    ])
    popups = enumerate_owned_popups(
        FOCUSED_HWND,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=desktop.control_type_of,
    )
    assert popups == []


def test_enumerate_skips_hidden_popup():
    desktop = FakeDesktop([
        FakeWindow(2004, owner=FOCUSED_HWND, class_name=CLASSIC_POPUP_CLASS_NAME,
                   visible=False),
    ])
    popups = enumerate_owned_popups(
        FOCUSED_HWND,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=desktop.control_type_of,
    )
    assert popups == []


def test_enumerate_skips_non_popup_class_non_menu_type():
    desktop = FakeDesktop([
        FakeWindow(2005, owner=FOCUSED_HWND, class_name="Notepad",
                   control_type=UIA_BUTTON),
    ])
    popups = enumerate_owned_popups(
        FOCUSED_HWND,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=desktop.control_type_of,
    )
    assert popups == []


def test_enumerate_skips_the_focused_window_itself():
    # The focused window is never its own owned popup even if it somehow has
    # the class/type signature.
    desktop = FakeDesktop([
        FakeWindow(FOCUSED_HWND, owner=FOCUSED_HWND,
                   class_name=CLASSIC_POPUP_CLASS_NAME),
    ])
    popups = enumerate_owned_popups(
        FOCUSED_HWND,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=desktop.control_type_of,
    )
    assert popups == []


def test_enumerate_tolerates_seam_errors_per_window():
    # A window whose owner/class lookup raises (closed between enumeration and
    # probe) is skipped, not fatal.
    def boom(_hwnd):
        raise OSError("window gone")

    desktop = FakeDesktop([
        FakeWindow(2006, owner=FOCUSED_HWND, class_name=CLASSIC_POPUP_CLASS_NAME),
        FakeWindow(2007, owner=FOCUSED_HWND, class_name=CLASSIC_POPUP_CLASS_NAME),
    ])
    # Make the FIRST window's class lookup raise.
    real_class_of = desktop.class_name_of

    def flaky_class_of(hwnd):
        if hwnd == 2006:
            raise OSError("gone")
        return real_class_of(hwnd)

    popups = enumerate_owned_popups(
        FOCUSED_HWND,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=flaky_class_of,
        visible_fn=desktop.is_visible,
        control_type_fn=desktop.control_type_of,
    )
    assert popups == [2007]


# ---------------------------------------------------------------------------
# walk_owned_popups: subtree walks under the SHARED CacheRequest.
# ---------------------------------------------------------------------------


def test_walk_owned_popups_uses_shared_cache_request():
    """design line 392: each popup is walked using the SAME CacheRequest object
    as the primary walk -- not a fresh one built per popup."""
    desktop = FakeDesktop([
        FakeWindow(2001, owner=FOCUSED_HWND, class_name=CLASSIC_POPUP_CLASS_NAME,
                   elements=[FakeCachedElement(name="Copy",
                                               control_type=UIA_MENUITEM,
                                               localized_control_type="menu item")]),
    ])
    automation = PopupAwareAutomation(desktop)
    shared_cache = FakeCacheRequest()

    results = walk_owned_popups(
        FOCUSED_HWND,
        automation=automation,
        cache_request=shared_cache,
        query_has_role=True,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=desktop.control_type_of,
    )
    assert len(results) == 1
    # No popup walk builds its own cache request -- the shared one is reused.
    assert automation.cache_requests_created == 0
    # Every popup-sourced match carries its owning popup HWND.
    popup_result = results[0]
    assert [m.name for m in popup_result.matches] == ["Copy"]
    assert all(m.source_window_hwnd == 2001 for m in popup_result.matches)


def test_walk_owned_popups_returns_one_walkresult_per_popup():
    desktop = FakeDesktop([
        FakeWindow(2001, owner=FOCUSED_HWND, class_name=CLASSIC_POPUP_CLASS_NAME,
                   elements=[FakeCachedElement(name="Cut", control_type=UIA_MENUITEM,
                                               localized_control_type="menu item")]),
        FakeWindow(2002, owner=FOCUSED_HWND, class_name=CLASSIC_POPUP_CLASS_NAME,
                   elements=[FakeCachedElement(name="Paste", control_type=UIA_MENUITEM,
                                               localized_control_type="menu item")]),
    ])
    automation = PopupAwareAutomation(desktop)
    shared_cache = FakeCacheRequest()

    results = walk_owned_popups(
        FOCUSED_HWND,
        automation=automation,
        cache_request=shared_cache,
        query_has_role=True,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=desktop.control_type_of,
    )
    assert len(results) == 2
    assert all(isinstance(r, WalkResult) for r in results)
    assert results[0].matches[0].source_window_hwnd == 2001
    assert results[1].matches[0].source_window_hwnd == 2002


def test_walk_owned_popups_no_popup_skips_element_from_handle():
    """ACCEPTANCE: no owned popup present -> no extra COM round-trip for the
    popup walk. ElementFromHandle is never called."""
    desktop = FakeDesktop([
        FakeWindow(3000, owner=9999, class_name="Other"),  # not owned, not popup
    ])
    automation = PopupAwareAutomation(desktop)
    shared_cache = FakeCacheRequest()

    results = walk_owned_popups(
        FOCUSED_HWND,
        automation=automation,
        cache_request=shared_cache,
        query_has_role=True,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=desktop.control_type_of,
    )
    assert results == []
    assert automation.element_from_handle_calls == 0


def test_walk_owned_popups_shares_deadline_and_truncation_fails_closed():
    """The popup walk honours the same per-request deadline; a popup walk the
    deadline cuts short is dropped (not appended as a partial set)."""
    desktop = FakeDesktop([
        FakeWindow(2001, owner=FOCUSED_HWND, class_name=CLASSIC_POPUP_CLASS_NAME,
                   elements=[FakeCachedElement(name="Copy", control_type=UIA_MENUITEM)]),
    ])
    automation = PopupAwareAutomation(desktop)
    shared_cache = FakeCacheRequest()

    results = walk_owned_popups(
        FOCUSED_HWND,
        automation=automation,
        cache_request=shared_cache,
        query_has_role=True,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=desktop.control_type_of,
        deadline=0.1,
        clock=lambda: 5.0,  # already past the deadline
    )
    # The popup walk was skipped (truncated) -> no usable popup result.
    assert results == []


def test_walk_owned_popups_spent_deadline_skips_enumeration_and_com_probe():
    """wh-n29v.47.2: when the shared deadline is ALREADY spent on entry,
    walk_owned_popups does no enumeration and issues NO live-COM control-type
    probe -- the expensive ElementFromHandle-backed control_type_fn that
    enumerate_owned_popups would otherwise call for every visible owned
    non-#32768 window. Returns []."""
    # A UIA-Menu popup (NOT classic #32768) is the case that forces the
    # control_type_fn COM probe in _is_owned_popup, so a missed guard would be
    # observable here.
    desktop = FakeDesktop([
        FakeWindow(3001, owner=FOCUSED_HWND, class_name="SomeMenuClass",
                   control_type=UIA_MENU,
                   elements=[FakeCachedElement(name="Copy",
                                               control_type=UIA_MENUITEM)]),
    ])
    automation = PopupAwareAutomation(desktop)

    enum_calls = {"n": 0}

    def counting_enumerate():
        enum_calls["n"] += 1
        return desktop.enumerate()

    control_type_calls = {"n": 0}

    def counting_control_type(hwnd):
        control_type_calls["n"] += 1
        return desktop.control_type_of(hwnd)

    results = walk_owned_popups(
        FOCUSED_HWND,
        automation=automation,
        cache_request=FakeCacheRequest(),
        query_has_role=True,
        enumerator=counting_enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=counting_control_type,
        deadline=0.1,
        clock=lambda: 5.0,  # already past the deadline on entry
    )

    assert results == []
    # Enumeration skipped entirely (deadline spent before it ran).
    assert enum_calls["n"] == 0
    # The expensive live-COM control-type probe never fired.
    assert control_type_calls["n"] == 0
    # And no popup subtree walk was attempted (no ElementFromHandle round trip).
    assert automation.element_from_handle_calls == 0


def test_enumerate_owned_popups_spent_deadline_skips_control_type_probe():
    """Direct enumerate_owned_popups guard: with the deadline spent, the live-COM
    control_type_fn probe is not called for a non-#32768 UIA-Menu window, so it
    is not detected as a popup. The cheap owner/class/visible seams may still be
    consulted; only the expensive COM probe is gated (wh-n29v.47.2)."""
    desktop = FakeDesktop([
        FakeWindow(3001, owner=FOCUSED_HWND, class_name="SomeMenuClass",
                   control_type=UIA_MENU),
    ])

    control_type_calls = {"n": 0}

    def counting_control_type(hwnd):
        control_type_calls["n"] += 1
        return desktop.control_type_of(hwnd)

    popups = enumerate_owned_popups(
        FOCUSED_HWND,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=counting_control_type,
        deadline=0.1,
        clock=lambda: 5.0,  # already past the deadline
    )

    # The COM probe was gated, so the UIA-Menu (non-#32768) window is NOT a popup.
    assert popups == []
    assert control_type_calls["n"] == 0


def test_enumerate_owned_popups_no_deadline_uses_real_control_type_probe():
    """No-deadline path is unchanged: the control_type_fn IS consulted and a
    UIA-Menu (non-#32768) owned window is still detected as a popup."""
    desktop = FakeDesktop([
        FakeWindow(3001, owner=FOCUSED_HWND, class_name="SomeMenuClass",
                   control_type=UIA_MENU),
    ])

    control_type_calls = {"n": 0}

    def counting_control_type(hwnd):
        control_type_calls["n"] += 1
        return desktop.control_type_of(hwnd)

    popups = enumerate_owned_popups(
        FOCUSED_HWND,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=counting_control_type,
    )

    assert popups == [3001]
    assert control_type_calls["n"] == 1


def test_walk_owned_popups_skips_stale_popup_window():
    """A popup that closes between enumeration and its walk (ElementFromHandle
    raises) is skipped, not fatal."""
    desktop = FakeDesktop([
        FakeWindow(2001, owner=FOCUSED_HWND, class_name=CLASSIC_POPUP_CLASS_NAME,
                   elements=[FakeCachedElement(name="Copy", control_type=UIA_MENUITEM)]),
        FakeWindow(2002, owner=FOCUSED_HWND, class_name=CLASSIC_POPUP_CLASS_NAME,
                   elements=[FakeCachedElement(name="Paste", control_type=UIA_MENUITEM)]),
    ])

    class FlakyAutomation(PopupAwareAutomation):
        def ElementFromHandle(self, hwnd):
            if hwnd == 2001:
                raise OSError("popup closed")
            return super().ElementFromHandle(hwnd)

    automation = FlakyAutomation(desktop)
    results = walk_owned_popups(
        FOCUSED_HWND,
        automation=automation,
        cache_request=FakeCacheRequest(),
        query_has_role=True,
        enumerator=desktop.enumerate,
        owner_fn=desktop.owner_of,
        class_name_fn=desktop.class_name_of,
        visible_fn=desktop.is_visible,
        control_type_fn=desktop.control_type_of,
    )
    # Only the surviving popup contributes.
    assert len(results) == 1
    assert results[0].matches[0].name == "Paste"


def test_walk_owned_popups_propagates_non_stale_hook_error():
    """A non-stale programming error from a forwarded hook (here ``score_hook``)
    that fires on popup match data PROPAGATES out of walk_owned_popups instead of
    being silently swallowed (wh-n29v.48.1).

    walk_window runs ``browser_correction_hook`` and ``score_hook`` over the
    popup's matches with no internal guard, so an ``AttributeError`` /
    ``ValueError`` from a hook bug reaches walk_owned_popups' per-popup catch.
    That catch is narrowed to the stale-window error classes (OSError / COMError),
    matching the deliberate narrow catch the primary walk (_walk_and_decide) and
    the restricted window fall-back (_run_fallback) use: a popup closing mid-walk
    is skipped, but a real programming error is NOT turned into a silently-dropped
    popup -- it surfaces (in production, as an execution_failed notice from the
    click handler) rather than degrading silently."""
    desktop = FakeDesktop([
        FakeWindow(2001, owner=FOCUSED_HWND, class_name=CLASSIC_POPUP_CLASS_NAME,
                   elements=[FakeCachedElement(name="Copy",
                                               control_type=UIA_MENUITEM)]),
    ])
    automation = PopupAwareAutomation(desktop)

    def buggy_score_hook(_matches):
        raise ValueError("hook bug triggered by popup match data")

    with pytest.raises(ValueError, match="hook bug triggered by popup match data"):
        walk_owned_popups(
            FOCUSED_HWND,
            automation=automation,
            cache_request=FakeCacheRequest(),
            query_has_role=True,
            score_hook=buggy_score_hook,
            enumerator=desktop.enumerate,
            owner_fn=desktop.owner_of,
            class_name_fn=desktop.class_name_of,
            visible_fn=desktop.is_visible,
            control_type_fn=desktop.control_type_of,
        )
