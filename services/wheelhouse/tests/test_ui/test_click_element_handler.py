"""End-to-end Input-side tests for UIActionHandler.click_element (wh-tab7j).

These exercise the by-name click handler that walks the focused window via
a real ElementFinder (driven over a fake IUIAutomation tree, the wh-mzpvx /
wh-en45t fake surface), runs the clear-winner rule, and -- on a clear winner --
runs ClickExecutor and emits exactly one ClickElementResponse on the response
queue.

Covered here (the Input half of the bead's five paths):
  * Happy path: a walked tree with one enabled "Cancel" button -> ok ->
    Invoke fires -> ClickElementResponse(status=ok, outcome=ok).
  * Walk-time disabled path: the only exact-name match is a disabled real
    control; decide() returns execution_failed:disabled BEFORE the executor
    runs (distinct from the Logic disabled_by_config short-circuit and from a
    click-time IsEnabled failure).
  * disabled-by-config short-circuit on the Input side (defence-in-depth: the
    handler refuses to walk when [click].enabled is false).
  * The handler emits exactly one response and never raises.

The Logic-side awaiter paths (timeout, malformed-response, the
response_timeout_ms regression, the config gate, notice forwarding) live in
test_click_flow.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.wheelhouse.shared.click_element import ClickElementResponse
from ui import uia_walker
from ui.element_types import ElementQuery

# Reuse the walker's fake cached-element / element-array surface.
from tests.test_uia_walker import FakeElementArray, FakeRect


_MOD = "ui.ui_action_handler"

UIA_BUTTON = uia_walker.UIA_BUTTON


class _FakeRawInvoke:
    """Raw POINTER(IUnknown) stand-in returned by GetCachedPattern: truthy,
    no direct Invoke, QueryInterface yields the element (which carries Invoke).
    Models the real comtypes shape the press path now QueryInterface's through
    (wh-click-invoke-on-element-not-pattern).
    """

    def __init__(self, element):
        self._element = element

    def QueryInterface(self, _iface):
        return self._element


class FakeClickableElement:
    """A fake element satisfying BOTH the walker's cached surface AND the
    executor's live surface (Invoke / CurrentIsEnabled / CurrentBoundingRect).

    The walker reads Cached* properties + GetCachedPattern; the executor calls
    Invoke() and reads CurrentIsEnabled / CurrentBoundingRectangle on the
    winner's control_ref (which the walker sets to this element). One fake
    serves both so a happy-path test drives the real walk -> decide -> click
    composition without a real display.
    """

    UIA_INVOKE_PATTERN_ID = 10000
    UIA_LEGACY_PATTERN_ID = 10018

    def __init__(self, *, name="", control_type=UIA_BUTTON,
                 localized_control_type="button", rect=None,
                 is_enabled=True, invoke_supported=True):
        self.CachedName = name
        self.CachedControlType = control_type
        self.CachedLocalizedControlType = localized_control_type
        self.CachedBoundingRectangle = rect or FakeRect(10, 20, 110, 70)
        self.CachedIsEnabled = is_enabled
        self._invoke_supported = invoke_supported
        # Executor live surface.
        self.CurrentIsEnabled = is_enabled
        self.CurrentBoundingRectangle = rect or FakeRect(10, 20, 110, 70)
        self.invoke_calls = 0

    def GetCachedPattern(self, pattern_id):
        # The walker reads this for eligibility (truthy => Invoke supported);
        # the executor presses through invoke_via_invoke_pattern, which fetches
        # the raw cached pattern here and QueryInterface's it to the typed
        # Invoke pattern (wh-click-invoke-on-element-not-pattern). Return a raw
        # stand-in whose QueryInterface yields this element, which exposes
        # Invoke -- so pattern.Invoke() drives the same invoke_calls / COMError
        # behaviour the executor tests rely on.
        if pattern_id == self.UIA_INVOKE_PATTERN_ID:
            return _FakeRawInvoke(self) if self._invoke_supported else None
        if pattern_id == self.UIA_LEGACY_PATTERN_ID:
            return None
        return None

    def GetCurrentPattern(self, _pattern_id):
        raise AssertionError(
            "press must use the cached pattern, not a live current read"
        )

    def Invoke(self):
        self.invoke_calls += 1


class FakeArrayTopLevel:
    """A fake top-level element whose FindAllBuildCache returns a fixed array."""

    def __init__(self, elements):
        self._array = FakeElementArray(elements)

    def FindAllBuildCache(self, _scope, _cond, _cache):
        return self._array


class FakeAutomation:
    """Minimal IUIAutomation stand-in for the walker's COM calls."""

    def CreateCacheRequest(self):
        class _Req:
            TreeScope = 0

            def AddProperty(self, _):
                pass

            def AddPattern(self, _):
                pass

        return _Req()

    def CreateTrueCondition(self):
        return object()

    def ElementFromHandle(self, _hwnd):
        raise AssertionError("tests pass a resolved top-level, never an HWND")


def _make_walk_fn(top_level_element):
    """Drive the REAL walk_window over a fake tree via the finder's walk_fn."""

    def _walk_fn(top_level, **kwargs):
        kwargs.pop("automation", None)
        return uia_walker.walk_window(
            top_level_element, automation=FakeAutomation(), **kwargs,
        )

    return _walk_fn


def _make_finder(top_level_element):
    from ui.element_finder import ElementFinder

    return ElementFinder(
        dpi_resolver=lambda _m: 96.0,
        monitor_resolver=lambda _b: 0,
        walk_fn=_make_walk_fn(top_level_element),
        window_enumerator=lambda: [],
    )


def _foreground():
    from ui.element_finder import ForegroundContext

    return ForegroundContext(
        foreground_window=1000,
        foreground_pid=4321,
        foreground_process_name="notepad.exe",
        foreground_window_creation_time=99,
        cursor_at_walk=(60, 45),
        cursor_monitor_id=0,
    )


def _executor():
    from ui.click_executor import ClickExecutor, ForegroundProbe

    def _probe():
        # Matches the snapshot foreground identity so pre-click verification
        # passes (HWND + PID + process + creation time all agree).
        return ForegroundProbe(
            window=1000, pid=4321, process_name="notepad.exe",
            window_creation_time=99,
        )

    return ClickExecutor(
        foreground_probe=_probe,
        on_screen_fn=lambda _x, _y: True,
    )


@pytest.fixture
def handler():
    """Build a UIActionHandler with specialist components mocked.

    [click].enabled defaults to True via ClickConfig.from_raw on an empty
    block, so the lazy finder builds unless a test overrides config.
    """
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):

        from ui.ui_action_handler import UIActionHandler

        q = MagicMock()
        h = UIActionHandler(response_queue=q, config={"ui_actions": {}})
        yield h


def _last_response(handler) -> ClickElementResponse:
    """Assert exactly one response was enqueued and parse it."""
    assert handler.response_queue.put.call_count == 1
    payload = handler.response_queue.put.call_args[0][0]
    assert payload["action"] == "click_element"
    assert payload["request_id"] == "req-click-1"
    return ClickElementResponse.from_dict(payload)


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_happy_path_walks_and_clicks(handler):
    button = FakeClickableElement(name="Cancel")
    top = FakeArrayTopLevel([button])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    handler._click_executor = _executor()

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.click_element(
            query=ElementQuery("cancel", "Button", None, None, "cancel"),
            trace_id="trace-happy",
            request_id="req-click-1",
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "ok"
    assert resp.matched_name == "Cancel"
    assert resp.trace_id == "trace-happy"
    assert resp.snapshot_id is not None
    assert resp.snapshot_summary is not None
    assert button.invoke_calls == 1


# ---------------------------------------------------------------------------
# Walk-time disabled path.
# ---------------------------------------------------------------------------


def test_walk_time_disabled_returns_execution_failed_disabled(handler):
    # The only exact-name match is a disabled real control. decide() marks it
    # the winner and downgrades to execution_failed:disabled BEFORE the
    # executor runs -- so Invoke is never called.
    button = FakeClickableElement(name="Cancel", is_enabled=False)
    top = FakeArrayTopLevel([button])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    handler._click_executor = _executor()

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.click_element(
            query=ElementQuery("cancel", "Button", None, None, "cancel"),
            trace_id="trace-dis",
            request_id="req-click-1",
        )

    resp = _last_response(handler)
    assert resp.outcome == "execution_failed"
    assert resp.reason == "disabled"
    assert resp.matched_name == "Cancel"
    assert button.invoke_calls == 0


# ---------------------------------------------------------------------------
# Disabled-by-config short-circuit (Input-side defence-in-depth).
# ---------------------------------------------------------------------------


def test_disabled_by_config_short_circuits_without_walk():
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):

        from ui.ui_action_handler import UIActionHandler

        q = MagicMock()
        h = UIActionHandler(
            response_queue=q,
            config={"ui_actions": {}, "click": {"enabled": False}},
        )

        walked = {"called": False}

        def _boom():
            walked["called"] = True
            raise AssertionError("must not walk when disabled by config")

        with patch(f"{_MOD}._capture_click_foreground", side_effect=_boom):
            h.click_element(
                query=ElementQuery("cancel", "Button", None, None, "cancel"),
                trace_id="trace-cfg",
                request_id="req-click-1",
            )

        assert walked["called"] is False
        assert q.put.call_count == 1
        payload = q.put.call_args[0][0]
        resp = ClickElementResponse.from_dict(payload)
        assert resp.outcome == "execution_failed"
        assert resp.reason == "disabled_by_config"


# ---------------------------------------------------------------------------
# Robustness: a non-ElementQuery is rejected, exactly one response, no raise.
# ---------------------------------------------------------------------------


def test_non_element_query_emits_malformed_query(handler):
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})

    handler.click_element(
        query="not a query",
        trace_id="trace-bad",
        request_id="req-click-1",
    )
    resp = _last_response(handler)
    assert resp.outcome == "execution_failed"
    assert resp.reason == "malformed_query"


# ---------------------------------------------------------------------------
# not_found and ambiguous Input-side branches (wh-9f3t.54.1).
# ---------------------------------------------------------------------------


def test_not_found_returns_not_found(handler):
    # The tree has a control, but none matches the spoken name.
    button = FakeClickableElement(name="Submit")
    top = FakeArrayTopLevel([button])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    handler._click_executor = _executor()

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.click_element(
            query=ElementQuery("cancel", "Button", None, None, "cancel"),
            trace_id="trace-nf",
            request_id="req-click-1",
        )

    resp = _last_response(handler)
    assert resp.outcome == "not_found"
    assert button.invoke_calls == 0


def test_ambiguous_returns_candidate_names(handler):
    # Two enabled controls share the spoken name at the same position, so
    # neither is a clear winner and the cursor-proximity tiebreaker abstains
    # (equal distance) -> ambiguous, carrying the matched names, no Invoke.
    a = FakeClickableElement(name="Cancel")
    b = FakeClickableElement(name="Cancel")
    top = FakeArrayTopLevel([a, b])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    handler._click_executor = _executor()

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.click_element(
            query=ElementQuery("cancel", "Button", None, None, "cancel"),
            trace_id="trace-amb",
            request_id="req-click-1",
        )

    resp = _last_response(handler)
    assert resp.outcome == "ambiguous"
    assert "Cancel" in resp.matched_names
    assert a.invoke_calls == 0
    assert b.invoke_calls == 0


def test_ambiguous_without_preset_click_config_does_not_misreport(handler):
    # Regression for wh-9f3t.54.1: when only the finder is injected and
    # _click_config is NOT pre-set, the ambiguous branch reads
    # self._click_config.notice_max_names. Before the fix this raised
    # AttributeError that the outer except swallowed into a misleading
    # invoke_com_error; _get_click_element_finder now lazily sets the config
    # on its early-return path, so the real ambiguous outcome surfaces.
    a = FakeClickableElement(name="Cancel")
    b = FakeClickableElement(name="Cancel")
    top = FakeArrayTopLevel([a, b])
    handler._click_element_finder = _make_finder(top)
    # Deliberately do NOT set handler._click_config.
    handler._click_executor = _executor()
    assert getattr(handler, "_click_config", None) is None

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.click_element(
            query=ElementQuery("cancel", "Button", None, None, "cancel"),
            trace_id="trace-amb-noc",
            request_id="req-click-1",
        )

    resp = _last_response(handler)
    assert resp.outcome == "ambiguous"
    assert resp.reason != "invoke_com_error"
    assert handler._click_config is not None


def test_click_element_in_handles_own_response_allowlist():
    from input_proc import _HANDLES_OWN_RESPONSE

    assert "click_element" in _HANDLES_OWN_RESPONSE


# ---------------------------------------------------------------------------
# Production-wiring regression: _get_click_executor injects a REAL coordinate
# seam, not the raising placeholder (wh-l4h.1 coordinate-click wiring slice).
# ---------------------------------------------------------------------------


class _InvokeComErrorElement(FakeClickableElement):
    """A clickable whose Invoke() raises a real allowlisted comtypes.COMError.

    UIA_E_NOTSUPPORTED (0x80040204) is on the executor's no-side-effect
    allowlist, so a real COMError carrying it drives Branch 2 of
    _handle_invoke_error straight into the coordinate-click fallback -- WITHOUT
    needing the enable_coordinate_click_on_com_error knob. Using a genuine
    comtypes.COMError (not a fake) means the executor's PRODUCTION
    com_error_predicate (_default_is_com_error) classifies it as a COM error,
    so this exercises the real construction from _get_click_executor end to end.
    """

    def Invoke(self):
        from comtypes import COMError  # type: ignore

        self.invoke_calls += 1
        # Signed form of 0x80040204 (UIA_E_NOTSUPPORTED). The details tuple is
        # the comtypes (descr, source, helpfile, helpcontext, progid) shape.
        raise COMError(-2147220988, "not supported", (None, None, None, None, None))


def test_get_click_executor_wires_real_coordinate_seam(handler):
    # Drive the allowlisted-HRESULT Branch 2: a real COMError(UIA_E_NOTSUPPORTED)
    # from Invoke, on an exact-name ("cancel") + Button match that passes the
    # stronger _coord_eligible gate, reaches the coordinate fallback. The
    # handler builds its OWN executor via _get_click_executor (we deliberately
    # do NOT preset handler._click_executor), so the only way the fallback can
    # succeed is if _get_click_executor injected the real _win32_coordinate_click
    # seam rather than leaving the raising placeholder. The module-level seam is
    # patched with a spy so the test stays headless (no real SendInput).
    button = _InvokeComErrorElement(name="Cancel")
    top = FakeArrayTopLevel([button])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    # Intentionally leave handler._click_executor unset so click_element calls
    # _get_click_executor, which must inject the real coordinate seam.
    assert getattr(handler, "_click_executor", None) is None

    spy_calls: list[tuple[int, int]] = []

    def _spy(x: int, y: int) -> tuple[bool, int]:
        spy_calls.append((x, y))
        return (True, 2)

    # _executor()'s probe matches _foreground(); reuse that matching probe so
    # pre-click verification (and the fallback's re-verification) pass. The
    # click-point hit-test seam (wh-explorer-navpane-click.1.1) is patched to
    # report the probe's own window (1000) as the root at the point so the
    # obstruction check passes headlessly -- _get_click_executor wires the
    # REAL WindowFromPoint query, which would see the test machine's actual
    # desktop. The UIA point-hits-winner layer (wh-explorer-navpane-click.1.4)
    # is patched the same way (its real ElementFromPoint would also see the
    # actual desktop); its handler-bound seam requires a usable automation
    # root, so a bare object stands in for one.
    handler._click_automation_root = object()
    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()), \
         patch(f"{_MOD}._win32_foreground_probe", new=_executor_probe()), \
         patch(f"{_MOD}._win32_on_screen", new=lambda _x, _y: True), \
         patch(f"{_MOD}._win32_root_window_at_point", new=lambda _x, _y: 1000), \
         patch(f"{_MOD}._uia_point_hits_winner", new=lambda _a, _w, _x, _y: True), \
         patch(f"{_MOD}._win32_coordinate_click", new=_spy):
        handler.click_element(
            query=ElementQuery("cancel", "Button", None, None, "cancel"),
            trace_id="trace-coord",
            request_id="req-click-1",
        )

    resp = _last_response(handler)
    # The real seam was injected and exercised: the click succeeded via the
    # coordinate fallback, the spy fired exactly once, and the placeholder
    # (which RAISES) was never used.
    assert resp.outcome == "ok"
    assert len(spy_calls) == 1
    assert button.invoke_calls == 1
    # The seam was called with the fresh bounding-rect centre the executor
    # re-read in _verify: FakeRect(10, 20, 110, 70) -> (x=10, y=20, w=100,
    # h=50) -> centre (10 + 100//2, 20 + 50//2) == (60, 45). Asserting the
    # real coordinates (not a getattr against a non-existent response field)
    # proves the executor handed the physical click point to the injected
    # seam (wh-9f3t.75.2).
    assert spy_calls[0] == (60, 45)


def _executor_probe():
    """Module-level foreground probe matching _foreground() for the real seam.

    _get_click_executor wires _win32_foreground_probe; the wiring test patches
    that name with this matching probe so the executor it builds verifies
    against the same identity _foreground() reports.
    """
    from ui.click_executor import ForegroundProbe

    def _probe() -> ForegroundProbe:
        return ForegroundProbe(
            window=1000, pid=4321, process_name="notepad.exe",
            window_creation_time=99,
        )

    return _probe


# ---------------------------------------------------------------------------
# Production-wiring regression: the owned-popup walker is turned ON in
# production (wh-n29v.71). _get_click_element_finder must inject a real
# IUIAutomation root AND the real walk_owned_popups default (no
# _inert_popup_walk gate), and _get_click_executor must inject the real
# IsWindowVisible / GetWindow(GW_OWNER) probe seams so ClickExecutor's
# popup-closed probe no longer fails closed. All three injections must land
# together for a popup-owned winner to be clickable.
# ---------------------------------------------------------------------------


def _capture_element_finder_kwargs(handler):
    """Build the production finder, capturing the ElementFinder kwargs.

    Patches the ElementFinder constructor (imported lazily inside
    _get_click_element_finder) with a recorder and create_automation with a
    sentinel so the test stays headless (no real COM). Returns the captured
    kwargs dict.
    """
    captured: dict = {}
    sentinel_root = object()

    class _RecordingFinder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    with patch("ui.element_finder.ElementFinder", _RecordingFinder), \
         patch("ui.uia_walker.create_automation", return_value=sentinel_root):
        result = handler._get_click_element_finder()

    return captured, sentinel_root, result


def test_get_click_element_finder_injects_real_automation_root(handler):
    captured, sentinel_root, _ = _capture_element_finder_kwargs(handler)
    # The finder receives a non-None IUIAutomation root -- the one
    # create_automation() built, not the constructor default of None.
    assert captured.get("automation") is sentinel_root
    assert captured["automation"] is not None


def test_get_click_element_finder_forwards_snapshot_store_capacity():
    # The validated [click] snapshot_store_capacity must reach the
    # ElementFinder constructor. Before the wiring fix the call site omitted
    # the kwarg, so the finder always fell back to its constructor default of
    # 4 regardless of config. Configure a NON-default value (16) and assert
    # the recorder captured 16, proving the knob is live.
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):
        from ui.ui_action_handler import UIActionHandler

        h = UIActionHandler(
            response_queue=MagicMock(),
            config={"ui_actions": {}, "click": {"snapshot_store_capacity": 16}},
        )

    captured, _sentinel_root, _ = _capture_element_finder_kwargs(h)
    assert captured.get("snapshot_store_capacity") == 16


def test_get_click_element_finder_uses_real_popup_walk_default(handler):
    captured, _sentinel_root, _ = _capture_element_finder_kwargs(handler)
    from ui import uia_walker
    import ui.ui_action_handler as ua_mod
    # The inert popup-walk gate is gone: either the kwarg is omitted (so the
    # ElementFinder default walk_owned_popups applies) or it is the real
    # walk_owned_popups. In NO case is an inert seam injected.
    if "popup_walk_fn" in captured:
        assert captured["popup_walk_fn"] is uia_walker.walk_owned_popups
    # The inert seam module symbol must no longer exist (removed with the gate).
    assert not hasattr(ua_mod, "_inert_popup_walk")


def test_get_click_element_finder_creates_root_lazily_and_reuses(handler):
    # The IUIAutomation root must be created lazily on the command-reader
    # thread (this method's call site), NOT in __init__, and reused across
    # clicks -- a COM object must be used on the apartment that created it.
    sentinel_root = object()
    create_calls = {"n": 0}

    def _fake_create():
        create_calls["n"] += 1
        return sentinel_root

    captured_roots: list = []

    class _RecordingFinder:
        def __init__(self, **kwargs):
            captured_roots.append(kwargs.get("automation"))

    with patch("ui.element_finder.ElementFinder", _RecordingFinder), \
         patch("ui.uia_walker.create_automation", side_effect=_fake_create):
        first = handler._get_click_element_finder()
        # Second call returns the memoised finder -- no second create_automation.
        second = handler._get_click_element_finder()

    assert first is second
    # create_automation ran exactly once (lazy + memoised), not per-click and
    # not in __init__.
    assert create_calls["n"] == 1
    assert captured_roots == [sentinel_root]


def test_get_click_element_finder_does_not_create_root_in_init():
    # Constructing the handler must NOT create the COM root: the root is
    # created lazily on the command-reader thread when click_element first
    # asks for the finder, never at __init__ time (apartment-threading fence).
    create_calls = {"n": 0}

    with patch("ui.uia_walker.create_automation",
               side_effect=lambda: create_calls.__setitem__("n", create_calls["n"] + 1) or object()), \
         patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):
        from ui.ui_action_handler import UIActionHandler

        UIActionHandler(response_queue=MagicMock(), config={"ui_actions": {}})

    assert create_calls["n"] == 0


def test_get_click_element_finder_disabled_does_not_create_root():
    # When voice clicking is disabled by config, the finder short-circuits to
    # None and must NOT build a COM root (no walk will ever run).
    create_calls = {"n": 0}

    with patch("ui.uia_walker.create_automation",
               side_effect=lambda: create_calls.__setitem__("n", create_calls["n"] + 1) or object()), \
         patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):
        from ui.ui_action_handler import UIActionHandler

        h = UIActionHandler(
            response_queue=MagicMock(),
            config={"ui_actions": {}, "click": {"enabled": False}},
        )
        assert h._get_click_element_finder() is None

    assert create_calls["n"] == 0


def test_get_click_executor_injects_real_popup_probe_seams(handler):
    # The production ClickExecutor must receive the real Win32 probe seams so
    # ClickExecutor._popup_still_open no longer fails closed for a still-open
    # owned popup.
    from ui import uia_walker

    captured: dict = {}

    class _RecordingExecutor:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    with patch("ui.click_executor.ClickExecutor", _RecordingExecutor):
        handler._get_click_executor()

    assert captured.get("popup_visible_fn") is uia_walker._default_is_window_visible
    assert captured.get("popup_owner_fn") is uia_walker._default_owner_of
    assert captured["popup_visible_fn"] is not None
    assert captured["popup_owner_fn"] is not None


def test_create_automation_failure_is_memoised_no_retry_storm(handler):
    # Finding wh-n29v.72.2: create_automation() RAISES on a permanently
    # degraded UIA host (locked-down / headless / broken UIAutomationCore).
    # The failure must be memoised at the session level so the latency-sensitive
    # command-reader loop does NOT re-run a failing CoCreateInstance on EVERY
    # click. The first failure short-circuits the finder to None; a SECOND call
    # returns None WITHOUT re-attempting create_automation().
    create_calls = {"n": 0}

    def _boom():
        create_calls["n"] += 1
        raise OSError("UIAutomationCore unavailable")

    with patch("ui.uia_walker.create_automation", side_effect=_boom):
        first = handler._get_click_element_finder()
        second = handler._get_click_element_finder()

    # (a) returns None on the failing host so click_element short-circuits.
    assert first is None
    assert second is None
    # (b) create_automation ran exactly ONCE across two finder builds -- the
    # failure is memoised, no per-utterance retry storm.
    assert create_calls["n"] == 1


def test_create_automation_none_return_is_treated_as_unavailable(handler):
    # Finding wh-n29v.73.3: create_automation() is contracted to RAISE on failure
    # and never return None (its CreateObject raises; the non-None assert is a
    # static-analysis guarantee that `python -O` strips). If a None ever reaches
    # _get_click_element_finder it must be treated IDENTICALLY to the raise path:
    # a memoised session-level give-up. Otherwise the handler would build an
    # ElementFinder(automation=None) and hand a None root to walk_owned_popups
    # (a partial-wiring state, not the _AUTOMATION_UNAVAILABLE give-up).
    create_calls = {"n": 0}

    def _none():
        create_calls["n"] += 1
        return None

    with patch("ui.uia_walker.create_automation", side_effect=_none):
        first = handler._get_click_element_finder()
        second = handler._get_click_element_finder()

    # (a) returns None on the bad-contract host so click_element short-circuits.
    assert first is None
    assert second is None
    # (b) create_automation ran exactly ONCE across two finder builds -- the None
    # return is memoised as _AUTOMATION_UNAVAILABLE, no per-utterance retry storm.
    assert create_calls["n"] == 1


def test_click_element_automation_unavailable_emits_distinct_reason(handler):
    # Finding wh-n29v.74.1 (deepseek reviewer_2): when the COM root cannot be
    # built (create_automation() raises on a degraded / headless / locked-down
    # UIAutomationCore host), the finder short-circuits to None via the
    # _AUTOMATION_UNAVAILABLE sentinel. click_element must then emit a DISTINCT
    # reason="automation_unavailable" -- NOT "disabled_by_config" -- so the user
    # notice does not falsely tell them to check config.toml [click] when
    # clicking IS enabled in config and the real cause is the machine.
    def _boom():
        raise OSError("UIAutomationCore unavailable")

    with patch("ui.uia_walker.create_automation", side_effect=_boom), \
         patch(f"{_MOD}._capture_click_foreground",
               side_effect=AssertionError("must not walk when COM unavailable")):
        handler.click_element(
            query=ElementQuery("cancel", "Button", None, None, "cancel"),
            trace_id="trace-com",
            request_id="req-click-1",
        )

    resp = _last_response(handler)
    assert resp.outcome == "execution_failed"
    assert resp.reason == "automation_unavailable"
    # The sentinel is what drove the distinct reason: confirm it is set so a
    # future refactor cannot satisfy this test by emitting the new reason on the
    # genuine config-disabled path too (which must stay "disabled_by_config",
    # asserted by test_disabled_by_config_short_circuits_without_walk).
    from ui.ui_action_handler import _AUTOMATION_UNAVAILABLE

    assert handler._click_automation_root is _AUTOMATION_UNAVAILABLE


def test_ambiguous_carries_finalist_item_ids_for_auto_open(handler):
    # wh-overlay-ambiguous-autoopen (found by deepseek): Logic's auto-open
    # gate (main.py, wh-n29v.111) requires response.ambiguous_item_ids to be
    # truthy, but this branch never set the field, so the numbered overlay
    # NEVER auto-opened on an ambiguous by-name click in production -- the
    # user always got the plain notice. The finalists' item ids must ride the
    # response (uncapped: notice_max_names caps only the notice wording) and
    # must resolve against the response's own snapshot summary, because Logic
    # stashes them as the item_id_filter for the AUTO_OPEN build.
    a = FakeClickableElement(name="Cancel")
    b = FakeClickableElement(name="Cancel")
    top = FakeArrayTopLevel([a, b])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    handler._click_executor = _executor()

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.click_element(
            query=ElementQuery("cancel", "Button", None, None, "cancel"),
            trace_id="trace-amb-ids",
            request_id="req-click-1",
        )

    resp = _last_response(handler)
    assert resp.outcome == "ambiguous"
    assert resp.ambiguous_item_ids is not None
    assert len(resp.ambiguous_item_ids) == 2
    assert resp.snapshot_summary is not None
    cancel_ids = {
        item.item_id
        for item in resp.snapshot_summary.items
        if item.name == "Cancel"
    }
    assert set(resp.ambiguous_item_ids) == cancel_ids
