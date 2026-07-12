"""Unit tests for the comtypes UI Automation tree walker (wh-en45t).

The walker's COM interop needs a live desktop, but its decision logic does
not. These tests drive the pure logic -- interactive-control-type filtering,
ElementMatch record construction from a cached element, the no-role path that
keeps static text, bounding-rectangle conversion, and the COM-object-lifetime
contract -- with fakes that model the cached-element / element-array surface.
The one test that genuinely needs a real UI Automation tree is marked skip.
"""

import gc

import pytest

from ui.element_types import ElementMatch
from ui import uia_walker
from ui.uia_walker import (
    INTERACTIVE_CONTROL_TYPE_IDS,
    SOURCE_UIA,
    UIA_BUTTON,
    UIA_EDIT,
    UIA_HYPERLINK,
    UIA_MENUITEM,
    WalkResult,
    element_match_from_cached,
    is_interactive_control_type,
    walk_window,
)


# ---------------------------------------------------------------------------
# Fakes modelling the cached COM element + element-array surface.
# ---------------------------------------------------------------------------

class FakeRect:
    """Mimics a UIA BoundingRectangle (left/top/right/bottom)."""

    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


# Pattern IDs the walker probes (stable Windows UIA values).
UIA_INVOKE_PATTERN_ID = 10000
UIA_LEGACY_PATTERN_ID = 10018


class FakeLegacyPattern:
    """Models a cached LegacyIAccessible pattern (CachedName surface)."""

    def __init__(self, name):
        self.CachedName = name


class FakeCachedElement:
    """Models the Cached* property surface the walker reads.

    Exposes CachedName, CachedControlType, CachedLocalizedControlType,
    CachedBoundingRectangle, CachedIsEnabled, and GetCachedPattern (Invoke
    + LegacyIAccessible) -- the same names a real comtypes
    IUIAutomationElement returned through FindAllBuildCache exposes.
    GetCurrentPattern raises so any accidental live read fails loudly.
    """

    def __init__(
        self,
        *,
        name="",
        control_type=UIA_BUTTON,
        localized_control_type="button",
        rect=None,
        is_enabled=True,
        invoke_supported=True,
        legacy_name=None,
        is_offscreen=False,
    ):
        self.CachedName = name
        self.CachedControlType = control_type
        self.CachedLocalizedControlType = localized_control_type
        self.CachedBoundingRectangle = rect or FakeRect(10, 20, 110, 70)
        self.CachedIsEnabled = is_enabled
        self.CachedIsOffscreen = is_offscreen
        self._invoke_supported = invoke_supported
        self._legacy_name = legacy_name

    def GetCachedPattern(self, pattern_id):
        # Distinguish the two cached patterns the walker probes:
        # Invoke (presence => invoke_supported) and LegacyIAccessible
        # (carries CachedName, the fallback name source). Returns None for
        # any other pattern, mirroring UIA's "pattern not supported" answer.
        if pattern_id == UIA_INVOKE_PATTERN_ID:
            return object() if self._invoke_supported else None
        if pattern_id == UIA_LEGACY_PATTERN_ID:
            if self._legacy_name is None:
                return None
            return FakeLegacyPattern(self._legacy_name)
        return None

    def GetCurrentPattern(self, pattern_id):
        # Real cached elements never reach a live getter (the walker gates
        # GetCurrentPattern behind _prefer_current, which is False here).
        # Raising makes any accidental live read fail loudly in tests.
        raise AssertionError(
            "GetCurrentPattern called on a cached element -- the walker must "
            "stay on the cached surface (no live COM round-trip)"
        )


class FakeCachedElementRaisingCurrent:
    """Models a real cached element: full Cached* surface PLUS a live
    ``CurrentName`` property whose getter raises.

    This is the regression fixture for wh-9f3t.6.1. On a real comtypes
    ``IUIAutomationElement``, ``CurrentName`` is a live cross-apartment COM
    read; merely *probing* it (e.g. ``hasattr(element, "CurrentName")``)
    fires that read. This fake makes the read fail loudly so any code path
    that touches ``CurrentName`` on the cached path raises ``AssertionError``.
    It deliberately does NOT set the ``_wh_prefer_current_surface`` marker, so
    ``_prefer_current`` must return False and the readers must stay on the
    Cached* surface, never evaluating ``CurrentName``.
    """

    def __init__(
        self,
        *,
        name="",
        control_type=UIA_BUTTON,
        localized_control_type="button",
        rect=None,
        is_enabled=True,
        invoke_supported=True,
        legacy_name=None,
        is_offscreen=False,
    ):
        self.CachedName = name
        self.CachedControlType = control_type
        self.CachedLocalizedControlType = localized_control_type
        self.CachedBoundingRectangle = rect or FakeRect(10, 20, 110, 70)
        self.CachedIsEnabled = is_enabled
        self.CachedIsOffscreen = is_offscreen
        self._invoke_supported = invoke_supported
        self._legacy_name = legacy_name

    @staticmethod
    def _raise_live_read(prop):
        # A live UIA read. Probing or reading any Current* prop on the cached
        # path is the bug.
        raise AssertionError(
            f"{prop} probed/read on a cached element -- the walker must "
            "never touch a Current* property on the production cached path"
        )

    # Guard ALL five Current* properties (not just CurrentName) so an
    # accidental Current-surface read in ANY of the six readers fails loudly,
    # rather than being swallowed by a reader's except-Exception and masked as
    # a value mismatch.
    @property
    def CurrentName(self):
        self._raise_live_read("CurrentName")

    @property
    def CurrentControlType(self):
        self._raise_live_read("CurrentControlType")

    @property
    def CurrentLocalizedControlType(self):
        self._raise_live_read("CurrentLocalizedControlType")

    @property
    def CurrentBoundingRectangle(self):
        self._raise_live_read("CurrentBoundingRectangle")

    @property
    def CurrentIsEnabled(self):
        self._raise_live_read("CurrentIsEnabled")

    def GetCachedPattern(self, pattern_id):
        if pattern_id == UIA_INVOKE_PATTERN_ID:
            return object() if self._invoke_supported else None
        if pattern_id == UIA_LEGACY_PATTERN_ID:
            if self._legacy_name is None:
                return None
            return FakeLegacyPattern(self._legacy_name)
        return None

    def GetCurrentPattern(self, pattern_id):
        raise AssertionError(
            "GetCurrentPattern called on a cached element -- the walker must "
            "stay on the cached surface (no live COM round-trip)"
        )


class FakeCurrentSurfaceElement:
    """Models the test-only Current* escape hatch.

    Sets the explicit ``_wh_prefer_current_surface`` marker and exposes ONLY
    the Current* surface (no Cached* attributes). Proves the marker actually
    selects the Current branch in each reader when a fake opts in.
    """

    def __init__(
        self,
        *,
        name="",
        control_type=UIA_BUTTON,
        localized_control_type="button",
        rect=None,
        is_enabled=True,
        invoke_supported=True,
    ):
        self._wh_prefer_current_surface = True
        self.CurrentName = name
        self.CurrentControlType = control_type
        self.CurrentLocalizedControlType = localized_control_type
        self.CurrentBoundingRectangle = rect or FakeRect(1, 2, 11, 22)
        self.CurrentIsEnabled = is_enabled
        self._invoke_supported = invoke_supported

    def GetCurrentPattern(self, pattern_id):
        if pattern_id == UIA_INVOKE_PATTERN_ID:
            return object() if self._invoke_supported else None
        return None


class FakeElementArray:
    """Models IUIAutomationElementArray (Length + GetElement(i))."""

    def __init__(self, elements):
        self._elements = list(elements)

    @property
    def Length(self):
        return len(self._elements)

    def GetElement(self, index):
        return self._elements[index]


# ---------------------------------------------------------------------------
# Interactive-control-type filtering.
# ---------------------------------------------------------------------------

def test_interactive_types_pass_when_role_specified():
    for control_type_id in INTERACTIVE_CONTROL_TYPE_IDS:
        assert is_interactive_control_type(control_type_id, query_has_role=True)


def test_static_text_dropped_when_role_specified():
    text_control_type = 50020  # UIA_TextControlTypeId
    pane_control_type = 50033  # UIA_PaneControlTypeId
    group_control_type = 50026  # UIA_GroupControlTypeId
    assert not is_interactive_control_type(text_control_type, query_has_role=True)
    assert not is_interactive_control_type(pane_control_type, query_has_role=True)
    assert not is_interactive_control_type(group_control_type, query_has_role=True)


def test_no_role_query_keeps_static_text():
    """Design: when the query specified no role, static text is kept."""
    text_control_type = 50020
    pane_control_type = 50033
    assert is_interactive_control_type(text_control_type, query_has_role=False)
    assert is_interactive_control_type(pane_control_type, query_has_role=False)
    assert is_interactive_control_type(UIA_BUTTON, query_has_role=False)


# ---------------------------------------------------------------------------
# Overlay opt-in: skip off-screen / zero-area controls
# (wh-overlay-stale-click-refresh).
# ---------------------------------------------------------------------------

def test_build_matches_skips_offscreen_when_opted_in():
    """The overlay opts into skip_offscreen_or_zero_area so it never numbers a
    control the user cannot see. An off-screen control is dropped and does NOT
    consume a display number; the kept controls stay numbered contiguously."""
    on1 = FakeCachedElement(name="One", control_type=UIA_BUTTON)
    off = FakeCachedElement(
        name="Hidden", control_type=UIA_BUTTON, is_offscreen=True
    )
    on2 = FakeCachedElement(name="Two", control_type=UIA_HYPERLINK)
    array = FakeElementArray([on1, off, on2])

    matches, truncated = uia_walker._build_matches_from_array(
        array, query_has_role=False, monitor_id=0,
        skip_offscreen_or_zero_area=True,
    )

    assert truncated is False
    assert [m.name for m in matches] == ["One", "Two"]
    assert [m.display_number for m in matches] == [1, 2]


def test_build_matches_skips_zero_area_when_opted_in():
    """A control whose cached rectangle has zero width or height is dropped when
    opted in -- its badge would paint at a zero/wrong spot and any click on it is
    refused at pre-click verification (bounds_invalid)."""
    sized = FakeCachedElement(name="Sized", rect=FakeRect(10, 20, 110, 70))
    zero_w = FakeCachedElement(name="ZeroWidth", rect=FakeRect(10, 20, 10, 70))
    zero_h = FakeCachedElement(name="ZeroHeight", rect=FakeRect(10, 20, 110, 20))
    array = FakeElementArray([sized, zero_w, zero_h])

    matches, _ = uia_walker._build_matches_from_array(
        array, query_has_role=False, monitor_id=0,
        skip_offscreen_or_zero_area=True,
    )

    assert [m.name for m in matches] == ["Sized"]
    assert matches[0].display_number == 1


def test_build_matches_keeps_offscreen_and_zero_area_when_not_opted_in():
    """Default (by-name find and popup walks) keeps the prior behavior: it does
    NOT drop off-screen or zero-area controls. The filter is opt-in, used by the
    overlay walk alone (wh-overlay-stale-click-refresh)."""
    on = FakeCachedElement(name="On", control_type=UIA_BUTTON)
    off = FakeCachedElement(name="Off", control_type=UIA_BUTTON, is_offscreen=True)
    zero = FakeCachedElement(name="Zero", rect=FakeRect(0, 0, 0, 0))
    array = FakeElementArray([on, off, zero])

    matches, _ = uia_walker._build_matches_from_array(
        array, query_has_role=False, monitor_id=0,
    )

    assert [m.name for m in matches] == ["On", "Off", "Zero"]
    assert [m.display_number for m in matches] == [1, 2, 3]


# ---------------------------------------------------------------------------
# ElementMatch record construction from a cached element.
# ---------------------------------------------------------------------------

def test_element_match_fields_from_cached_element():
    element = FakeCachedElement(
        name="  Cancel  ",
        control_type=UIA_BUTTON,
        localized_control_type="button",
        rect=FakeRect(10, 20, 110, 70),
        is_enabled=True,
        invoke_supported=True,
    )
    match = element_match_from_cached(element, display_number=3, monitor_id=2)

    assert isinstance(match, ElementMatch)
    assert match.name == "Cancel"  # stripped
    assert match.role == "button"
    assert match.bounds == (10, 20, 100, 50)  # left,top,(right-left),(bottom-top)
    assert match.monitor_id == 2
    assert match.display_number == 3
    assert match.source == SOURCE_UIA
    assert match.invoke_supported is True
    assert match.is_enabled is True
    # Scorer fills these in later -- the walker leaves them neutral.
    assert match.score == 0.0
    assert match.is_eligible is False
    # control_ref is the cached element itself (executor Invokes it later).
    assert match.control_ref is element


def test_element_match_populates_control_type_id_from_cached_control_type():
    """wh-l4h.1.12: element_match_from_cached stamps the numeric UIA
    control-type id read from the element's CachedControlType when the caller
    does not pass it explicitly. This is the locale-invariant role signal the
    browser DOM-folding predicates compare."""
    element = FakeCachedElement(
        name="Open", control_type=UIA_HYPERLINK, localized_control_type="Link"
    )
    match = element_match_from_cached(element, display_number=1)
    assert match.control_type_id == UIA_HYPERLINK
    # The localized role string is independent of the id.
    assert match.role == "Link"


def test_element_match_control_type_id_explicit_overrides_cached_read():
    """The build loop already reads CachedControlType for the interactive
    filter and threads that value in to avoid a redundant second cached read;
    an explicitly passed control_type_id is used verbatim."""
    element = FakeCachedElement(name="Open", control_type=UIA_BUTTON)
    match = element_match_from_cached(
        element, display_number=1, control_type_id=50026
    )
    assert match.control_type_id == 50026


def test_element_match_default_item_id():
    element = FakeCachedElement(name="OK")
    match = element_match_from_cached(element, display_number=5)
    assert match.item_id == f"{SOURCE_UIA}-5"


def test_element_match_explicit_item_id():
    element = FakeCachedElement(name="OK")
    match = element_match_from_cached(element, display_number=5, item_id="custom-id")
    assert match.item_id == "custom-id"


def test_element_match_disabled_and_no_invoke():
    element = FakeCachedElement(
        name="Grayed", is_enabled=False, invoke_supported=False
    )
    match = element_match_from_cached(element, display_number=1)
    assert match.is_enabled is False
    assert match.invoke_supported is False


def test_no_invoke_does_not_trigger_live_read():
    """FINDING 1: a cached Invoke pattern of None must yield
    invoke_supported=False WITHOUT falling through to a live
    GetCurrentPattern call.

    FakeCachedElement.GetCurrentPattern raises AssertionError, so if the
    walker fell through to the live getter this test would error. The
    record builds cleanly and reports invoke_supported=False.
    """
    element = FakeCachedElement(name="Label", invoke_supported=False)
    match = element_match_from_cached(element, display_number=1)
    assert match.invoke_supported is False


class _NullComPointer:
    """Models the NULL POINTER(IUnknown) comtypes returns from
    GetCachedPattern for an unsupported pattern: falsy in a boolean test,
    but NOT Python ``None`` (reviewer_1/codex finding .2.1, confirmed live).
    """

    def __bool__(self):
        return False


class _NullInvokeCachedElement(FakeCachedElement):
    """A cached element whose Invoke pattern resolves to a NULL pointer
    (falsy, not None) rather than ``None``. Models the real comtypes answer
    for a control that does not support Invoke.
    """

    def GetCachedPattern(self, pattern_id):
        if pattern_id == UIA_INVOKE_PATTERN_ID:
            return _NullComPointer()
        if pattern_id == UIA_LEGACY_PATTERN_ID:
            return None
        return None


def test_null_pointer_invoke_pattern_is_not_supported():
    """reviewer_1/codex finding .2.1: a NULL COM pointer (falsy but
    ``is not None``) must yield invoke_supported=False. The walk-time support
    check must use the same truthiness guard the press path uses, or it would
    stamp invoke_supported=True for a control that cannot be pressed -- which
    inflates the scorer's Invoke bonus and breaks the clear-winner invariant.

    GetCurrentPattern raises on the cached element, so a fall-through to a live
    read would error rather than silently pass.
    """
    element = _NullInvokeCachedElement(name="Label")
    match = element_match_from_cached(element, display_number=1)
    assert match.invoke_supported is False


def test_cached_path_never_probes_current_name():
    """FINDING wh-9f3t.6.1: the cached readers must never probe a Current*
    COM property on a real cached element.

    FakeCachedElementRaisingCurrent has the full Cached* surface and a
    ``CurrentName`` property whose getter raises AssertionError, and it does
    NOT set the ``_wh_prefer_current_surface`` marker. ``_prefer_current``
    must return False without touching ``CurrentName``, so the readers stay on
    the Cached* surface and the record builds cleanly. If ``_prefer_current``
    probed ``CurrentName`` (the old hasattr bug), this test would error.
    """
    element = FakeCachedElementRaisingCurrent(
        name="  Cancel  ",
        control_type=UIA_BUTTON,
        localized_control_type="button",
        rect=FakeRect(10, 20, 110, 70),
        is_enabled=True,
        invoke_supported=True,
    )
    # The gate itself must not raise (it must not touch CurrentName).
    assert uia_walker._prefer_current(element) is False
    match = element_match_from_cached(element, display_number=1)
    assert match.name == "Cancel"
    assert match.role == "button"
    assert match.bounds == (10, 20, 100, 50)
    assert match.is_enabled is True
    assert match.invoke_supported is True


def test_current_surface_escape_hatch_reads_current_branch():
    """The marker-gated Current* escape hatch is reachable: a fake that sets
    ``_wh_prefer_current_surface = True`` and exposes only the Current*
    surface must be read via the Current branch in each reader."""
    element = FakeCurrentSurfaceElement(
        name="  Open  ",
        control_type=UIA_HYPERLINK,
        localized_control_type="link",
        rect=FakeRect(1, 2, 11, 22),
        is_enabled=False,
        invoke_supported=True,
    )
    assert uia_walker._prefer_current(element) is True
    match = element_match_from_cached(element, display_number=1)
    assert match.name == "Open"  # read from CurrentName
    assert match.role == "link"  # read from CurrentLocalizedControlType
    assert match.bounds == (1, 2, 10, 20)  # from CurrentBoundingRectangle
    assert match.is_enabled is False  # from CurrentIsEnabled
    assert match.invoke_supported is True  # from GetCurrentPattern(Invoke)


class FakeCurrentLegacyPattern:
    """Models a live LegacyIAccessible pattern's Current* name surface.

    The live comtypes ``IUIAutomationLegacyIAccessiblePattern`` exposes the
    accessible name as ``CurrentName`` (not ``Name``). This fake exposes only
    that attribute so a reader that wrongly read ``Name`` would see None.
    """

    def __init__(self, name):
        self.CurrentName = name


class FakeCurrentSurfaceLegacyElement:
    """Current-surface escape-hatch element with an EMPTY CurrentName whose
    LegacyIAccessible pattern carries the name on its ``CurrentName`` attr.

    Forces ``_cached_name`` to fall through to ``_cached_legacy_name`` on the
    Current branch, exercising FINDING 1's ``attr = "CurrentName"`` fix.
    """

    def __init__(self, *, legacy_name):
        self._wh_prefer_current_surface = True
        self.CurrentName = ""  # empty -> fall through to legacy fallback
        self.CurrentControlType = UIA_BUTTON
        self.CurrentLocalizedControlType = "button"
        self.CurrentBoundingRectangle = FakeRect(0, 0, 10, 10)
        self.CurrentIsEnabled = True
        self._legacy_name = legacy_name

    def GetCurrentPattern(self, pattern_id):
        if pattern_id == UIA_INVOKE_PATTERN_ID:
            return object()
        if pattern_id == UIA_LEGACY_PATTERN_ID:
            return FakeCurrentLegacyPattern(self._legacy_name)
        return None


def test_current_surface_legacy_name_read_via_current_attr():
    """FINDING wh-9f3t.6.1 (DeepSeek FINDING 1): on the Current-surface
    branch, ``_cached_legacy_name`` must read the pattern's ``CurrentName``
    attribute, not ``Name``.

    The element has an empty ``CurrentName`` so ``_cached_name`` falls through
    to the legacy fallback; the legacy pattern exposes the name only as
    ``CurrentName``. Reading ``Name`` (the pre-fix bug) would yield "" and
    silently drop the legacy name.
    """
    element = FakeCurrentSurfaceLegacyElement(legacy_name="  Submit  ")
    assert uia_walker._prefer_current(element) is True
    match = element_match_from_cached(element, display_number=1)
    assert match.name == "Submit"  # stripped legacy name via CurrentName


def test_legacy_name_fallback_when_uia_name_empty():
    """FINDING 2: an element with an empty UIA Name but a cached
    LegacyIAccessible name uses the Legacy name (cached surface, no live
    read)."""
    element = FakeCachedElement(name="", legacy_name="  Submit  ")
    match = element_match_from_cached(element, display_number=1)
    assert match.name == "Submit"  # stripped Legacy name


def test_uia_name_preferred_over_legacy_name():
    """The Legacy name is a fallback only -- a present UIA Name wins."""
    element = FakeCachedElement(name="Save", legacy_name="legacy-save")
    match = element_match_from_cached(element, display_number=1)
    assert match.name == "Save"


def test_empty_name_and_no_legacy_yields_empty():
    element = FakeCachedElement(name="", legacy_name=None)
    match = element_match_from_cached(element, display_number=1)
    assert match.name == ""


def test_rect_as_tuple_is_converted():
    element = FakeCachedElement(name="X")
    element.CachedBoundingRectangle = (5, 6, 25, 36)  # left,top,right,bottom tuple
    match = element_match_from_cached(element, display_number=1)
    assert match.bounds == (5, 6, 20, 30)


def test_malformed_rect_yields_zero_bounds():
    element = FakeCachedElement(name="X")
    element.CachedBoundingRectangle = None
    match = element_match_from_cached(element, display_number=1)
    assert match.bounds == (0, 0, 0, 0)


# ---------------------------------------------------------------------------
# walk_window over a fake automation/array (no live desktop).
# ---------------------------------------------------------------------------

class FakeCacheRequest:
    def __init__(self):
        self.properties = []
        self.patterns = []
        self.TreeScope = None

    def AddProperty(self, prop_id):
        self.properties.append(prop_id)

    def AddPattern(self, pattern_id):
        self.patterns.append(pattern_id)


class FakeAutomation:
    """Minimal IUIAutomation stand-in for walk_window.

    Records that exactly one cache request and one FindAllBuildCache run.
    """

    def __init__(self, element_array):
        self._element_array = element_array
        self.cache_requests_created = 0
        self.true_conditions_created = 0

    def CreateCacheRequest(self):
        self.cache_requests_created += 1
        return FakeCacheRequest()

    def CreateTrueCondition(self):
        self.true_conditions_created += 1
        return object()


class FakeTopLevel:
    def __init__(self, element_array):
        self._element_array = element_array
        self.find_all_build_cache_calls = 0

    def FindAllBuildCache(self, tree_scope, condition, cache_request):
        self.find_all_build_cache_calls += 1
        return self._element_array


def _mixed_array():
    return FakeElementArray(
        [
            FakeCachedElement(name="Save", control_type=UIA_BUTTON,
                              localized_control_type="button"),
            FakeCachedElement(name="Home", control_type=UIA_HYPERLINK,
                              localized_control_type="link"),
            # Static text -- dropped when a role is queried.
            FakeCachedElement(name="Heading", control_type=50020,
                              localized_control_type="text"),
            FakeCachedElement(name="File", control_type=UIA_MENUITEM,
                              localized_control_type="menu item"),
        ]
    )


def test_walk_window_single_cache_and_single_findall():
    array = _mixed_array()
    automation = FakeAutomation(array)
    top_level = FakeTopLevel(array)

    result = walk_window(top_level, automation=automation, query_has_role=True)

    assert automation.cache_requests_created == 1
    assert top_level.find_all_build_cache_calls == 1
    # Three interactive controls kept; static text dropped.
    names = [m.name for m in result.matches]
    assert names == ["Save", "Home", "File"]
    # Display numbers are 1-based over the kept elements.
    assert [m.display_number for m in result.matches] == [1, 2, 3]


def test_walk_window_no_role_keeps_static_text():
    array = _mixed_array()
    automation = FakeAutomation(array)
    top_level = FakeTopLevel(array)

    result = walk_window(top_level, automation=automation, query_has_role=False)
    names = [m.name for m in result.matches]
    assert names == ["Save", "Home", "Heading", "File"]


def test_walk_window_applies_hooks_in_order():
    array = FakeElementArray(
        [FakeCachedElement(name="Save", control_type=UIA_BUTTON)]
    )
    automation = FakeAutomation(array)
    top_level = FakeTopLevel(array)
    calls = []

    def browser_hook(matches):
        calls.append("browser")
        return matches

    def score_hook(matches):
        calls.append("score")
        # Simulate the scorer filling in eligibility/score.
        return [
            ElementMatch(**{**m.__dict__, "score": 0.9, "is_eligible": True})
            for m in matches
        ]

    result = walk_window(
        top_level,
        automation=automation,
        browser_correction_hook=browser_hook,
        score_hook=score_hook,
    )
    assert calls == ["browser", "score"]  # browser corrections before scoring
    assert result.matches[0].score == 0.9
    assert result.matches[0].is_eligible is True


def test_walk_window_hooks_default_to_identity():
    array = FakeElementArray(
        [FakeCachedElement(name="Save", control_type=UIA_BUTTON)]
    )
    automation = FakeAutomation(array)
    top_level = FakeTopLevel(array)
    result = walk_window(top_level, automation=automation)
    assert len(result.matches) == 1
    assert result.matches[0].score == 0.0
    assert result.matches[0].is_eligible is False


# ---------------------------------------------------------------------------
# Walk deadline: pre-walk bound + post-FindAllBuildCache fail-closed truncation.
#
# walk_window takes a SINGLE ABSOLUTE monotonic deadline (not a per-call
# duration) so the caller can bound a whole multi-walk request with one budget
# (wh-9f3t.54.2 FINDING 1). The single FindAllBuildCache COM call is not
# interruptible mid-call; the levers are (1) a PRE-walk check that skips the COM
# call entirely when the deadline has already passed, and (2) a POST-call check
# that short-circuits the per-element loop. In BOTH cases the WalkResult is
# flagged deadline_truncated=True and its matches are EMPTY -- a partial prefix
# is never handed downstream (FINDING 2 fail-closed). The injected clock drives
# both deterministically.
# ---------------------------------------------------------------------------

class CountingTopLevel:
    """A fake top-level that counts FindAllBuildCache calls (pre-walk bound)."""

    def __init__(self, element_array):
        self._element_array = element_array
        self.find_all_build_cache_calls = 0

    def FindAllBuildCache(self, tree_scope, condition, cache_request):
        self.find_all_build_cache_calls += 1
        return self._element_array


def _ten_button_array():
    return FakeElementArray(
        [
            FakeCachedElement(name=f"Btn{i}", control_type=UIA_BUTTON,
                              localized_control_type="button")
            for i in range(10)
        ]
    )


def test_walk_no_deadline_processes_every_element():
    """Backward compat: with deadline=None the walk processes the whole array
    regardless of the clock, and is not flagged truncated."""
    array = _ten_button_array()
    automation = FakeAutomation(array)
    top_level = FakeTopLevel(array)

    # A clock that jumps wildly must not matter when there is no deadline.
    ticks = iter([0.0, 100.0, 200.0, 300.0])
    result = walk_window(
        top_level,
        automation=automation,
        query_has_role=True,
        deadline=None,
        clock=lambda: next(ticks, 999.0),
    )
    assert len(result.matches) == 10
    assert result.deadline_truncated is False


def test_walk_post_call_truncation_fails_closed_empty_matches():
    """Once the monotonic clock passes the absolute deadline mid-loop, the walk
    FAILS CLOSED: it returns deadline_truncated=True with EMPTY matches (the
    partial prefix is discarded) rather than handing a partial set downstream
    (FINDING 2)."""
    array = _ten_button_array()
    automation = FakeAutomation(array)
    top_level = FakeTopLevel(array)

    # Absolute deadline = 1.0s. clock advances 0.3s/read: pre-walk check 0.3
    # (< 1.0 -> proceed), loop sees 0.6, 0.9 (< 1.0 -> build), then 1.2
    # (>= 1.0 -> short-circuit). Some elements were built, but they are
    # discarded by the fail-closed contract.
    state = {"t": 0.0}

    def clock():
        state["t"] += 0.3
        return round(state["t"], 6)

    result = walk_window(
        top_level,
        automation=automation,
        query_has_role=True,
        deadline=1.0,
        clock=clock,
    )
    # FindAllBuildCache still ran once (the call itself is not interruptible).
    assert top_level.find_all_build_cache_calls == 1
    # FAIL CLOSED: truncated flag set, no partial matches leak downstream.
    assert result.deadline_truncated is True
    assert result.matches == []


def test_walk_pre_walk_bound_skips_findall_when_deadline_passed():
    """Pre-walk bound: if the absolute deadline has ALREADY passed before the
    single blocking FindAllBuildCache call, skip the call entirely and return an
    empty WalkResult flagged deadline_truncated=True so the caller fails closed
    to not_found."""
    array = _ten_button_array()
    automation = FakeAutomation(array)
    top_level = CountingTopLevel(array)

    # The pre-walk check reads a clock value (5.0) already past the absolute
    # deadline (0.1).
    result = walk_window(
        top_level,
        automation=automation,
        query_has_role=True,
        deadline=0.1,
        clock=lambda: 5.0,
    )
    # The expensive COM call was skipped.
    assert top_level.find_all_build_cache_calls == 0
    assert result.matches == []
    assert result.deadline_truncated is True
    assert isinstance(result, WalkResult)


def test_walk_deadline_in_future_processes_everything():
    """A deadline comfortably in the future does not truncate: all elements are
    built and the flag stays False."""
    array = _ten_button_array()
    automation = FakeAutomation(array)
    top_level = FakeTopLevel(array)
    result = walk_window(
        top_level,
        automation=automation,
        query_has_role=True,
        deadline=1000.0,
        clock=lambda: 0.0,
    )
    assert len(result.matches) == 10
    assert result.deadline_truncated is False


class ElementFromHandleSpyAutomation:
    """IUIAutomation stand-in that records ElementFromHandle calls.

    Production focused/fall-back walks pass an HWND int, which walk_window
    resolves via automation.ElementFromHandle -- itself a live UIA COM call
    that can block/fail against a hung or elevated window. This spy asserts
    that resolution is NOT attempted when the deadline is already past.
    """

    def __init__(self, element_array):
        self._element_array = element_array
        self.element_from_handle_calls = 0
        self.cache_requests_created = 0

    def ElementFromHandle(self, _hwnd):
        self.element_from_handle_calls += 1
        return FakeTopLevel(self._element_array)

    def CreateCacheRequest(self):
        self.cache_requests_created += 1
        return FakeCacheRequest()

    def CreateTrueCondition(self):
        return object()


def test_walk_int_hwnd_skips_element_from_handle_when_deadline_passed():
    """FINDING B (wh-9f3t.73.2): for an int HWND top_level, an already-expired
    deadline must short-circuit BEFORE automation.ElementFromHandle -- no live
    UIA COM call against a possibly-hung window. The pre-walk check is hoisted
    ahead of HWND resolution."""
    array = _ten_button_array()
    automation = ElementFromHandleSpyAutomation(array)

    result = walk_window(
        12345,  # an HWND int, as production focused/fall-back walks pass
        automation=automation,
        query_has_role=True,
        deadline=0.1,
        clock=lambda: 5.0,  # already past the deadline
    )
    # ElementFromHandle was NOT called -- no COM touched after the deadline.
    assert automation.element_from_handle_calls == 0
    # Nor was the cache request built.
    assert automation.cache_requests_created == 0
    assert result.matches == []
    assert result.deadline_truncated is True


def test_walk_int_hwnd_resolves_element_from_handle_within_budget():
    """Control case: with budget remaining, the int HWND IS resolved via
    ElementFromHandle and the walk proceeds normally."""
    array = _ten_button_array()
    automation = ElementFromHandleSpyAutomation(array)

    result = walk_window(
        12345,
        automation=automation,
        query_has_role=True,
        deadline=1000.0,
        clock=lambda: 0.0,
    )
    assert automation.element_from_handle_calls == 1
    assert len(result.matches) == 10
    assert result.deadline_truncated is False


# ---------------------------------------------------------------------------
# COM-object-lifetime contract: gc.collect() between walk and click.
# ---------------------------------------------------------------------------

def test_walk_result_holds_strong_references():
    array = _mixed_array()
    automation = FakeAutomation(array)
    top_level = FakeTopLevel(array)

    result = walk_window(top_level, automation=automation, query_has_role=True)

    # All four required strong references are held together.
    assert result._keepalive_automation is automation
    assert result._keepalive_element_array is array
    assert result._keepalive_top_level_element is top_level
    assert result._keepalive_cache_request is not None
    assert result.is_alive()


def test_references_survive_gc_collect():
    """The design's lifetime test: gc.collect() between walk return and the
    simulated click must NOT release the required COM objects.

    Stand-in COM objects are tracked by weakref. After dropping every local
    handle except the WalkResult and running gc.collect(), the objects the
    WalkResult holds must still be alive (weakref not cleared). The match's
    control_ref -- the object the executor would Invoke -- must also survive.
    """
    import weakref

    array = _mixed_array()
    automation = FakeAutomation(array)
    top_level = FakeTopLevel(array)

    result = walk_window(top_level, automation=automation, query_has_role=True)

    array_ref = weakref.ref(array)
    automation_ref = weakref.ref(automation)
    top_level_ref = weakref.ref(top_level)
    control_ref_ref = weakref.ref(result.matches[0].control_ref)

    # Drop every local handle except the WalkResult -- this models the gap
    # between walk return and ClickExecutor.click().
    del array, automation, top_level

    gc.collect()

    assert result.is_alive()
    assert array_ref() is not None, "element array was released by GC"
    assert automation_ref() is not None, "IUIAutomation root was released by GC"
    assert top_level_ref() is not None, "top-level element was released by GC"
    assert control_ref_ref() is not None, "cached control_ref was released by GC"


def test_dropping_walk_result_allows_collection():
    """Sanity check the lifetime test is meaningful: once the WalkResult is
    gone, GC may reclaim the held objects (the references really were the
    only thing keeping them alive)."""
    import weakref

    array = FakeElementArray(
        [FakeCachedElement(name="Save", control_type=UIA_BUTTON)]
    )
    automation = FakeAutomation(array)
    top_level = FakeTopLevel(array)
    result = walk_window(top_level, automation=automation)

    array_ref = weakref.ref(array)
    del array, automation, top_level, result
    gc.collect()
    assert array_ref() is None


# ---------------------------------------------------------------------------
# Live-desktop path (requires a real UI Automation tree).
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason="Requires a live Windows desktop + STA COM apartment with a real "
    "top-level window; covered by integration testing, not unit tests."
)
def test_walk_real_window_smoke():  # pragma: no cover
    import win32gui

    hwnd = win32gui.GetForegroundWindow()
    automation = uia_walker.create_automation()
    result = walk_window(hwnd, automation=automation, query_has_role=True)
    assert result.is_alive()
    assert isinstance(result.matches, list)


# ---------------------------------------------------------------------------
# invoke_via_invoke_pattern (wh-click-invoke-on-element-not-pattern)
#
# A real IUIAutomationElement has NO Invoke method -- Invoke lives on
# IUIAutomationInvokePattern. The element's ``GetCachedPattern(id)`` /
# ``GetCurrentPattern(id)`` return a raw ``POINTER(IUnknown)`` that must be
# QueryInterface'd to the typed Invoke pattern before ``Invoke()`` exists.
# Two reproduced-live facts drive these fakes (reviewer_0 findings .1.1/.1.3):
#   * The ``*As`` variants (GetCachedPatternAs / GetCurrentPatternAs) return a
#     raw int, so calling ``.Invoke()`` on the result raises
#     AttributeError("'int' object has no attribute 'Invoke'"). The fix uses
#     the non-As getters plus QueryInterface.
#   * comtypes returns a NULL ``POINTER(IUnknown)`` object (truthy-test False,
#     but ``is not None`` True) for an unsupported pattern, so the absence
#     guard must be a truthiness check, not ``is not None``.
# The fakes below model exactly that surface: a raw pointer object whose
# ``QueryInterface`` yields the typed pattern, a null-pointer object that is
# falsy, and an element exposing only ``GetCachedPattern`` / ``GetCurrentPattern``
# (NO direct Invoke, NO *As variants). A fake with a direct ``.Invoke()`` would
# mask the very bug under test, so none is provided.
# ---------------------------------------------------------------------------

class FakeInvokePattern:
    """Models the typed IUIAutomationInvokePattern: just an Invoke() method."""

    def __init__(self, raises=None):
        self.invoke_calls = 0
        self._raises = raises

    def Invoke(self):
        self.invoke_calls += 1
        if self._raises is not None:
            raise self._raises


class FakeRawPattern:
    """Models the raw POINTER(IUnknown) a pattern getter returns.

    Truthy, exposes ``QueryInterface`` (which yields the typed pattern), and
    has NO direct ``Invoke`` -- exactly like a real comtypes IUnknown pointer.
    """

    def __init__(self, typed, qi_raises=None):
        self._typed = typed
        self._qi_raises = qi_raises
        self.qi_calls = 0

    def QueryInterface(self, _iface):
        self.qi_calls += 1
        if self._qi_raises is not None:
            raise self._qi_raises
        return self._typed


class FakeNullPattern:
    """Models the NULL POINTER(IUnknown) comtypes returns for an unsupported
    pattern: falsy in a boolean test, but NOT Python ``None``. The helper must
    treat it as absent via a truthiness check.
    """

    def __bool__(self):
        return False


class FakeInvokableElement:
    """A cached element exposing the non-As pattern getters only.

    ``GetCachedPattern`` / ``GetCurrentPattern`` return whatever the test wired
    (a FakeRawPattern, a FakeNullPattern, or None). There is deliberately NO
    direct ``Invoke`` and NO ``*As`` variant.
    """

    def __init__(self, cached=None, current=None):
        self._cached = cached
        self._current = current
        self.cached_calls = 0
        self.current_calls = 0

    def GetCachedPattern(self, pattern_id):
        self.cached_calls += 1
        return self._cached

    def GetCurrentPattern(self, pattern_id):
        self.current_calls += 1
        return self._current


def test_invoke_via_pattern_uses_cached_pattern():
    pattern = FakeInvokePattern()
    raw = FakeRawPattern(pattern)
    element = FakeInvokableElement(cached=raw)

    uia_walker.invoke_via_invoke_pattern(element)

    assert pattern.invoke_calls == 1
    # The pattern was reached through QueryInterface, not a direct element call.
    assert raw.qi_calls == 1
    # The cached pattern was present, so the live fallback was not consulted.
    assert element.current_calls == 0


def test_invoke_via_pattern_falls_back_to_current_when_cache_absent():
    pattern = FakeInvokePattern()
    raw = FakeRawPattern(pattern)
    element = FakeInvokableElement(cached=None, current=raw)

    uia_walker.invoke_via_invoke_pattern(element)

    assert pattern.invoke_calls == 1
    assert element.cached_calls == 1
    assert element.current_calls == 1


def test_invoke_via_pattern_treats_null_pointer_as_absent():
    """A NULL COM pointer (falsy but ``is not None``) must be treated as
    absent. The cached getter returns the null pointer; with no live pattern
    the helper must raise rather than QueryInterface a null pointer (which
    would raise ``ValueError: NULL COM pointer access`` live).
    """
    element = FakeInvokableElement(cached=FakeNullPattern(), current=None)

    with pytest.raises(uia_walker.InvokePatternUnavailable):
        uia_walker.invoke_via_invoke_pattern(element)


def test_invoke_via_pattern_falls_back_when_cached_is_null_pointer():
    """A null cached pointer falls through to the live current pattern."""
    pattern = FakeInvokePattern()
    element = FakeInvokableElement(
        cached=FakeNullPattern(), current=FakeRawPattern(pattern)
    )

    uia_walker.invoke_via_invoke_pattern(element)

    assert pattern.invoke_calls == 1
    assert element.current_calls == 1


def test_invoke_via_pattern_raises_when_no_pattern_available():
    element = FakeInvokableElement(cached=None, current=None)

    with pytest.raises(uia_walker.InvokePatternUnavailable):
        uia_walker.invoke_via_invoke_pattern(element)


def test_invoke_via_pattern_does_not_call_invoke_on_element_directly():
    """The element must never be asked to Invoke itself -- that is the
    original bug. A bare element with no pattern surface and no Invoke
    method must raise InvokePatternUnavailable, not silently succeed and
    not AttributeError on a direct element.Invoke().
    """
    class BareElement:
        pass

    with pytest.raises(uia_walker.InvokePatternUnavailable):
        uia_walker.invoke_via_invoke_pattern(BareElement())


def test_invoke_via_pattern_raises_when_raw_has_no_queryinterface():
    """A raw object lacking QueryInterface cannot yield a typed pattern, so
    the helper treats it as absent and raises -- it must never fall back to a
    direct ``.Invoke()`` on the raw object (the regression fence for the
    int-return / direct-Invoke shape).
    """
    class RawWithoutQI:
        def Invoke(self):  # present to prove the helper does NOT call it
            raise AssertionError("must reach Invoke only via QueryInterface")

    element = FakeInvokableElement(cached=RawWithoutQI(), current=None)

    with pytest.raises(uia_walker.InvokePatternUnavailable):
        uia_walker.invoke_via_invoke_pattern(element)


def test_invoke_via_pattern_falls_back_when_cached_queryinterface_raises():
    """reviewer_2/deepseek finding .3.1: when the cached raw pointer is truthy
    but its QueryInterface raises, the helper must treat the cached fetch as
    absent and fall back to the live current pattern -- not let the exception
    propagate and skip the fallback. Here the live pattern is present, so the
    press succeeds through it.
    """
    good = FakeInvokePattern()
    element = FakeInvokableElement(
        cached=FakeRawPattern(typed=None, qi_raises=RuntimeError("QI failed")),
        current=FakeRawPattern(good),
    )

    uia_walker.invoke_via_invoke_pattern(element)

    assert good.invoke_calls == 1
    assert element.current_calls == 1


def test_invoke_via_pattern_raises_unavailable_when_queryinterface_raises_and_no_live():
    """reviewer_2/deepseek finding .3.1: a cached QueryInterface failure with no
    live pattern must surface as InvokePatternUnavailable (the distinct
    no-pattern tag), not as the raw QueryInterface exception. Keeping every
    _typed_invoke_pattern failure path returning None preserves the contract.
    """
    element = FakeInvokableElement(
        cached=FakeRawPattern(typed=None, qi_raises=RuntimeError("QI failed")),
        current=None,
    )

    with pytest.raises(uia_walker.InvokePatternUnavailable):
        uia_walker.invoke_via_invoke_pattern(element)


# ---------------------------------------------------------------------------
# do_default_action_via_legacy_pattern (wh-click-dda-wiring / wh-l4h.1.17)
#
# The DoDefaultAction press fallback for a control with no UIA Invoke pattern.
# Like Invoke, DoDefaultAction lives on the typed pattern
# (IUIAutomationLegacyIAccessiblePattern), reached by fetching the raw
# POINTER(IUnknown) and QueryInterface-ing it. Two differences from the Invoke
# press drive these fakes:
#   * It reads the LIVE current pattern (GetCurrentPattern), not the cached one:
#     this is the cold fallback, and the DefaultAction property is not in the
#     walk CacheRequest, so a cached pattern's CachedDefaultAction would raise.
#   * It MUST short-circuit on an empty DefaultAction (NoDefaultAction) BEFORE
#     calling DoDefaultAction() -- accDoDefaultAction returning a no-op success
#     would otherwise fake a press that never fired.
# The raw-pointer / null-pointer / element fakes from the Invoke suite above are
# reused; only the typed pattern differs (DoDefaultAction + CurrentDefaultAction
# instead of Invoke).
# ---------------------------------------------------------------------------

class FakeLegacyActionPattern:
    """Models the typed IUIAutomationLegacyIAccessiblePattern surface the
    DoDefaultAction press uses: a ``CurrentDefaultAction`` property and a
    ``DoDefaultAction()`` method. ``do_default_action_calls`` records presses.
    """

    def __init__(self, default_action="Press", raises=None):
        self._default_action = default_action
        self._raises = raises
        self.do_default_action_calls = 0

    @property
    def CurrentDefaultAction(self):
        return self._default_action

    def DoDefaultAction(self):
        self.do_default_action_calls += 1
        if self._raises is not None:
            raise self._raises


def test_dda_via_pattern_presses_through_current_pattern():
    pattern = FakeLegacyActionPattern(default_action="Press")
    raw = FakeRawPattern(pattern)
    element = FakeInvokableElement(current=raw)

    uia_walker.do_default_action_via_legacy_pattern(element)

    assert pattern.do_default_action_calls == 1
    # The press was reached through QueryInterface, not a direct element call.
    assert raw.qi_calls == 1
    # The live current pattern is used; the cached getter is never consulted.
    assert element.current_calls == 1
    assert element.cached_calls == 0


def test_dda_via_pattern_raises_unavailable_when_no_pattern():
    element = FakeInvokableElement(current=None)

    with pytest.raises(uia_walker.DoDefaultActionUnavailable):
        uia_walker.do_default_action_via_legacy_pattern(element)


def test_dda_via_pattern_treats_null_pointer_as_absent():
    """A NULL COM pointer (falsy but ``is not None``) must be treated as absent
    and raise rather than QueryInterface a null pointer.
    """
    element = FakeInvokableElement(current=FakeNullPattern())

    with pytest.raises(uia_walker.DoDefaultActionUnavailable):
        uia_walker.do_default_action_via_legacy_pattern(element)


def test_dda_via_pattern_raises_no_default_action_when_default_empty():
    """The Legacy pattern is present but its DefaultAction is empty: raise
    NoDefaultAction and NEVER call DoDefaultAction() -- pressing would risk a
    no-op success that the executor would misread as a real press.
    """
    pattern = FakeLegacyActionPattern(default_action="")
    element = FakeInvokableElement(current=FakeRawPattern(pattern))

    with pytest.raises(uia_walker.NoDefaultAction):
        uia_walker.do_default_action_via_legacy_pattern(element)

    assert pattern.do_default_action_calls == 0


def test_dda_via_pattern_unreadable_default_action_is_no_default_action():
    """A Legacy pattern whose DefaultAction read raises is failed CLOSED as
    NoDefaultAction (pattern resolved, no usable default action) rather than
    pressed blind. The original read error is preserved as the NoDefaultAction
    cause, so a transient COM read failure is distinguishable in diagnostics
    from a control that genuinely has an empty default action.
    """
    read_error = RuntimeError("default-action property read failed")

    class RaisingDefaultAction(FakeLegacyActionPattern):
        @property
        def CurrentDefaultAction(self):
            raise read_error

    pattern = RaisingDefaultAction()
    element = FakeInvokableElement(current=FakeRawPattern(pattern))

    with pytest.raises(uia_walker.NoDefaultAction) as excinfo:
        uia_walker.do_default_action_via_legacy_pattern(element)

    assert pattern.do_default_action_calls == 0
    # The unreadable-property branch chains the original error; the genuinely
    # empty-default-action branch does not (there is no underlying exception).
    assert excinfo.value.__cause__ is read_error


def test_dda_via_pattern_raises_unavailable_when_raw_has_no_queryinterface():
    """A raw object lacking QueryInterface cannot yield a typed pattern, so the
    helper treats it as absent and raises -- it must never fall back to a direct
    ``.DoDefaultAction()`` on the raw object.
    """
    class RawWithoutQI:
        def DoDefaultAction(self):  # present to prove it is NOT called directly
            raise AssertionError("must reach DoDefaultAction only via QueryInterface")

    element = FakeInvokableElement(current=RawWithoutQI())

    with pytest.raises(uia_walker.DoDefaultActionUnavailable):
        uia_walker.do_default_action_via_legacy_pattern(element)


def test_dda_via_pattern_does_not_call_dda_on_element_directly():
    """A bare element with no pattern surface must raise
    DoDefaultActionUnavailable, never AttributeError on a direct element call.
    """
    class BareElement:
        pass

    with pytest.raises(uia_walker.DoDefaultActionUnavailable):
        uia_walker.do_default_action_via_legacy_pattern(BareElement())


def test_dda_via_pattern_propagates_com_error_from_press():
    """A COM error from DoDefaultAction() itself must PROPAGATE -- the executor
    consults its HRESULT against the no-side-effect allowlist. Swallowing it
    would hide a may-have-fired press and could fake success.
    """
    boom = RuntimeError("DoDefaultAction failed")
    pattern = FakeLegacyActionPattern(default_action="Press", raises=boom)
    element = FakeInvokableElement(current=FakeRawPattern(pattern))

    with pytest.raises(RuntimeError, match="DoDefaultAction failed"):
        uia_walker.do_default_action_via_legacy_pattern(element)

    assert pattern.do_default_action_calls == 1


def test_dda_via_pattern_getter_raises_is_unavailable():
    """When the pattern getter itself raises (an uncached/unsupported pattern
    raises rather than returning null), _typed_legacy_pattern treats it as
    absent, so the press raises DoDefaultActionUnavailable -- not the raw getter
    exception. Mirrors the _typed_invoke_pattern getter-raises branch.
    """
    class RaisingGetterElement:
        def GetCurrentPattern(self, _pattern_id):
            raise RuntimeError("GetCurrentPattern failed")

    with pytest.raises(uia_walker.DoDefaultActionUnavailable):
        uia_walker.do_default_action_via_legacy_pattern(RaisingGetterElement())


def test_dda_via_pattern_queryinterface_raises_is_unavailable():
    """A truthy raw pointer whose QueryInterface raises is treated as absent, so
    the press raises DoDefaultActionUnavailable rather than letting the
    QueryInterface exception propagate. Mirrors the _typed_invoke_pattern
    QueryInterface-raises branch (there is no cached fallback here, so a raising
    current QueryInterface yields unavailable directly).
    """
    element = FakeInvokableElement(
        current=FakeRawPattern(typed=None, qi_raises=RuntimeError("QI failed"))
    )

    with pytest.raises(uia_walker.DoDefaultActionUnavailable):
        uia_walker.do_default_action_via_legacy_pattern(element)


def test_dda_via_pattern_unresolvable_interface_class_is_unavailable(monkeypatch):
    """When the LegacyIAccessible interface class cannot be resolved (the gen
    module is absent), _legacy_pattern_class returns None and the press fails
    closed with DoDefaultActionUnavailable rather than QueryInterface-ing to a
    None interface. Mirrors the _typed_invoke_pattern interface-class-None branch.
    """
    monkeypatch.setattr(uia_walker, "_legacy_pattern_class", lambda: None)
    pattern = FakeLegacyActionPattern(default_action="Press")
    element = FakeInvokableElement(current=FakeRawPattern(pattern))

    with pytest.raises(uia_walker.DoDefaultActionUnavailable):
        uia_walker.do_default_action_via_legacy_pattern(element)

    # The interface never resolved, so the press was never attempted.
    assert pattern.do_default_action_calls == 0


# ---------------------------------------------------------------------------
# Transient stale-window retry on the PRIMARY focused-window walk
# (wh-overlay-walk-com-retry).
#
# A focus change to a Chromium/Brave window can leave the window's UIA element
# momentarily virtualized or destroyed, so the live ElementFromHandle /
# FindAllBuildCache call raises UIA_E_ELEMENTNOTAVAILABLE (0x80040201) even
# though the window is still the foreground and resolves cleanly a moment later.
# The popup walker already tolerates a stale window (it SKIPS the popup); the
# primary walk did not retry at all, so one transient error failed the whole
# walk -- the numbers never repainted on the browser window. A tree walk is
# read-only and idempotent, so re-resolving the element and re-walking is
# side-effect-free and safe to retry.
#
# The retry is OPT-IN via the walk_window(transient_retries=N) parameter, which
# defaults to 0. The overlay's primary walk opts in; the by-name walk and every
# owned-popup walk stay at 0 (reviewer_0 finding wh-overlay-walk-com-retry.1.2,
# popups must skip a closed menu fast, not retry it). The element resolution and
# the FindAllBuildCache both run inside the retry, so a stale error from
# ElementFromHandle on the int-HWND production path is retried too (reviewer_0
# finding wh-overlay-walk-com-retry.1.1). These tests model the transient error
# with OSError, matching test_popup_walker.py (raise OSError("popup closed")).
# ---------------------------------------------------------------------------


class FakeTopLevelFlaky:
    """FindAllBuildCache raises a stale-window error the first ``fail_times``
    calls, then returns the array (a transient error that clears on retry)."""

    def __init__(self, element_array, fail_times, error=None):
        self._element_array = element_array
        self._fail_times = fail_times
        self._error = error if error is not None else OSError("element gone")
        self.find_all_build_cache_calls = 0

    def FindAllBuildCache(self, tree_scope, condition, cache_request):
        self.find_all_build_cache_calls += 1
        if self.find_all_build_cache_calls <= self._fail_times:
            raise self._error
        return self._element_array


def test_walk_window_retries_transient_stale_error_then_succeeds():
    # One transient stale-window error, then success: an opted-in walk must
    # retry and return the real matches rather than letting the error fail it.
    array = _mixed_array()
    automation = FakeAutomation(array)
    top_level = FakeTopLevelFlaky(array, fail_times=1)

    result = walk_window(
        top_level,
        automation=automation,
        query_has_role=True,
        transient_retries=uia_walker.WALK_TRANSIENT_RETRY_ATTEMPTS,
    )

    # Retried once (two calls total) and recovered the matches.
    assert top_level.find_all_build_cache_calls == 2
    assert [m.name for m in result.matches] == ["Save", "Home", "File"]
    assert result.deadline_truncated is False


def test_walk_window_default_does_not_retry():
    # Default transient_retries=0: a stale-window error is NOT retried, so the
    # by-name walk and every owned-popup walk keep their prior behaviour -- one
    # attempt, then the error propagates to the caller, which (for a popup)
    # skips it fast (reviewer_0 finding wh-overlay-walk-com-retry.1.2).
    array = _mixed_array()
    automation = FakeAutomation(array)
    top_level = FakeTopLevelFlaky(array, fail_times=1)

    with pytest.raises(OSError, match="element gone"):
        walk_window(top_level, automation=automation, query_has_role=True)

    assert top_level.find_all_build_cache_calls == 1


def test_walk_window_retries_element_resolution_failure(monkeypatch):
    # The production overlay walk passes an int HWND, so element_from_hwnd
    # (ElementFromHandle) runs live every attempt and is exactly where a
    # virtualized browser window can raise. That resolution is INSIDE the retry
    # try, so a stale error there is retried too (reviewer_0 finding .1.1). The
    # original tests passed fake objects, which never exercised this path.
    array = _mixed_array()
    automation = FakeAutomation(array)
    resolved = FakeTopLevel(array)  # healthy element once resolution succeeds
    resolve_calls = {"n": 0}

    def flaky_element_from_hwnd(auto, hwnd):
        resolve_calls["n"] += 1
        if resolve_calls["n"] == 1:
            raise OSError("window not available yet")
        return resolved

    monkeypatch.setattr(uia_walker, "element_from_hwnd", flaky_element_from_hwnd)

    # top_level is an int HWND (the production shape).
    result = walk_window(
        1234,
        automation=automation,
        query_has_role=True,
        transient_retries=uia_walker.WALK_TRANSIENT_RETRY_ATTEMPTS,
    )

    # Resolution failed once, was retried, then succeeded: two resolution calls,
    # one successful FindAllBuildCache, real matches.
    assert resolve_calls["n"] == 2
    assert resolved.find_all_build_cache_calls == 1
    assert [m.name for m in result.matches] == ["Save", "Home", "File"]


def test_walk_window_reraises_after_exhausting_retries():
    # A PERSISTENT stale error (every attempt fails) is re-raised after the
    # bounded retries, preserving the pre-retry contract (the caller's
    # never-raise wrapper then fails the walk -- same as before the retry).
    array = _mixed_array()
    automation = FakeAutomation(array)
    retries = uia_walker.WALK_TRANSIENT_RETRY_ATTEMPTS
    total_attempts = retries + 1
    top_level = FakeTopLevelFlaky(array, fail_times=total_attempts)

    with pytest.raises(OSError, match="element gone"):
        walk_window(
            top_level,
            automation=automation,
            query_has_role=True,
            transient_retries=retries,
        )

    # Exactly the bounded number of attempts -- no unbounded retry loop.
    assert top_level.find_all_build_cache_calls == total_attempts


def test_walk_window_does_not_retry_non_stale_programming_error():
    # A non-stale exception (a programming error, here ValueError) must NOT be
    # retried even with retries enabled -- it propagates immediately on the first
    # attempt so a real bug surfaces, matching
    # test_walk_owned_popups_propagates_non_stale_hook_error.
    array = _mixed_array()
    automation = FakeAutomation(array)
    top_level = FakeTopLevelFlaky(
        array, fail_times=99, error=ValueError("walker bug")
    )

    with pytest.raises(ValueError, match="walker bug"):
        walk_window(
            top_level,
            automation=automation,
            query_has_role=True,
            transient_retries=uia_walker.WALK_TRANSIENT_RETRY_ATTEMPTS,
        )

    # One attempt only: not on the stale-window retry path.
    assert top_level.find_all_build_cache_calls == 1


def test_walk_window_retry_respects_deadline():
    # Retries must not overrun the per-request deadline. The first attempt
    # fails AND consumes the whole budget; the next attempt's deadline re-check
    # abandons the walk (deadline_truncated) instead of burning more attempts.
    array = _mixed_array()
    automation = FakeAutomation(array)
    holder = {"t": 0.0}

    class FakeTopLevelStaleAdvancingClock:
        def __init__(self):
            self.find_all_build_cache_calls = 0

        def FindAllBuildCache(self, tree_scope, condition, cache_request):
            self.find_all_build_cache_calls += 1
            holder["t"] += 100.0  # this attempt "consumed" 100ms
            raise OSError("element gone")

    top_level = FakeTopLevelStaleAdvancingClock()

    # Entry clock 0 < deadline 50; after the first failed attempt the clock is
    # 100 >= 50, so the retry is abandoned before a second FindAllBuildCache.
    result = walk_window(
        top_level,
        automation=automation,
        query_has_role=True,
        deadline=50.0,
        clock=lambda: holder["t"],
        transient_retries=uia_walker.WALK_TRANSIENT_RETRY_ATTEMPTS,
    )

    assert result.deadline_truncated is True
    assert result.matches == []
    assert top_level.find_all_build_cache_calls == 1
