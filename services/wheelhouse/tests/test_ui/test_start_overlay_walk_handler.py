"""End-to-end Input-side tests for UIActionHandler.start_overlay_walk (wh-n29v.37).

``start_overlay_walk`` is the standalone "show numbers" overlay build request:
it walks the focused window FROM SCRATCH (no prior ``click_element`` request),
numbers every interactive control 1..K, and emits exactly one
``StartOverlayWalkResponse`` carrying the fresh snapshot id + summary and the
echoed ``overlay_session_id`` / ``paint_generation`` / ``trace_id``.

Covered here:
  * ok with targets: a walked tree of interactive controls -> outcome=ok,
    a populated summary numbered 1..K, status=ok.
  * no_targets: a walked tree with zero interactive controls -> outcome=
    no_targets (empty summary), status=ok.
  * execution_failed: the walk reports a deadline truncation -> outcome=
    execution_failed, status=ok.
  * disabled-overlay-config short-circuit: the handler refuses to walk when
    overlay_enabled_effective is False (Input-side defence-in-depth), and the
    by-name finder/walk is never invoked.
  * generation/trace echo: overlay_session_id, paint_generation, trace_id are
    echoed verbatim.
  * the handler emits exactly one Schema A response carrying request_id +
    action, and never raises (an unexpected error maps to outcome=error).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.wheelhouse.shared.start_overlay_walk import (
    StartOverlayWalkResponse,
)
from ui import uia_walker

# Reuse the walker's fake cached-element / element-array surface.
from tests.test_uia_walker import FakeCachedElement, FakeElementArray, FakeRect


_MOD = "ui.ui_action_handler"

UIA_BUTTON = uia_walker.UIA_BUTTON
UIA_HYPERLINK = uia_walker.UIA_HYPERLINK
# A non-interactive control type (static text) -- dropped by the interactive
# filter when query_has_role=True, which the overlay walk uses for non-browser
# processes so only clickable controls get numbered.
UIA_TEXT = 50020


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


def _el(name, *, control_type=UIA_BUTTON, role="button", rect=None):
    return FakeCachedElement(
        name=name,
        control_type=control_type,
        localized_control_type=role,
        rect=rect or FakeRect(10, 20, 110, 70),
    )


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


@pytest.fixture
def handler():
    """Build a UIActionHandler with specialist components mocked.

    [click] (and the overlay) defaults to enabled via ClickConfig.from_raw on
    an empty block, so the lazy finder builds unless a test overrides config.
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


def _last_response(handler) -> StartOverlayWalkResponse:
    """Assert exactly one response was enqueued and parse it."""
    assert handler.response_queue.put.call_count == 1
    payload = handler.response_queue.put.call_args[0][0]
    assert payload["action"] == "start_overlay_walk"
    assert payload["request_id"] == "req-walk-1"
    return StartOverlayWalkResponse.from_dict(payload)


# ---------------------------------------------------------------------------
# ok with targets.
# ---------------------------------------------------------------------------


def test_ok_with_targets_numbers_all_interactive_controls(handler):
    # Three interactive controls + one static-text control. The static text is
    # dropped by the interactive filter; the three clickables are numbered 1..3.
    # Distinct side-by-side rects: same-rect fakes would read as one visual
    # control and collapse to one badge (wh-overlay-nested-dupes).
    top = FakeArrayTopLevel([
        _el("Save", rect=FakeRect(10, 20, 110, 70)),
        _el("Open", control_type=UIA_HYPERLINK, role="hyperlink",
            rect=FakeRect(120, 20, 220, 70)),
        _el("not a control", control_type=UIA_TEXT, role="text"),
        _el("Cancel", rect=FakeRect(230, 20, 330, 70)),
    ])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=2,
            trace_id="trace-ok",
            request_id="req-walk-1",
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "ok"
    assert resp.snapshot_id is not None
    assert resp.snapshot_summary is not None
    # All three interactive controls are present, numbered 1..3 contiguous.
    items = resp.snapshot_summary.items
    assert len(items) == 3
    assert [i.display_number for i in items] == [1, 2, 3]
    assert {i.name for i in items} == {"Save", "Open", "Cancel"}
    # Generation + trace echoed verbatim.
    assert resp.overlay_session_id == 5
    assert resp.paint_generation == 2
    assert resp.trace_id == "trace-ok"


# ---------------------------------------------------------------------------
# no_targets.
# ---------------------------------------------------------------------------


def test_no_targets_when_no_interactive_controls(handler):
    # A window whose only control is static text -> the interactive filter
    # drops it -> zero numbered items -> outcome=no_targets.
    top = FakeArrayTopLevel([
        _el("just a label", control_type=UIA_TEXT, role="text"),
    ])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=1,
            paint_generation=0,
            trace_id="trace-nt",
            request_id="req-walk-1",
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "no_targets"
    # A snapshot was still produced (the walk ran), and any summary it carries
    # has no items.
    if resp.snapshot_summary is not None:
        assert resp.snapshot_summary.items == []


# ---------------------------------------------------------------------------
# execution_failed (walk-time failure).
# ---------------------------------------------------------------------------


def test_execution_failed_on_walk_deadline_truncation(handler):
    # A walk that the deadline cut short returns deadline_truncated=True ->
    # the overlay walk maps it to execution_failed (the focused window could
    # not be fully walked). After reviewer_0 finding 38.2 the HANDLER anchors
    # the deadline (dequeue instant + _click_config.walk_deadline_ms), not the
    # finder's self-anchor, so drive truncation through that path: a dequeue
    # instant of 0.0 with the default 2500 ms bound makes the absolute deadline
    # 2.5, and the finder's frozen clock at 1000.0 trips walk_window's pre-walk
    # bound immediately (1000.0 >= 2.5).
    from ui.element_finder import ElementFinder

    top = FakeArrayTopLevel([_el("Save")])

    finder = ElementFinder(
        dpi_resolver=lambda _m: 96.0,
        monitor_resolver=lambda _b: 0,
        walk_fn=_make_walk_fn(top),
        window_enumerator=lambda: [],
        clock=lambda: 1000.0,
    )
    handler._click_element_finder = finder
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=3,
            paint_generation=1,
            trace_id="trace-ef",
            request_id="req-walk-1",
            command_dequeue_monotonic=0.0,
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "execution_failed"
    assert resp.reason is not None


# ---------------------------------------------------------------------------
# Disabled-overlay-config short-circuit (Input-side defence-in-depth).
# ---------------------------------------------------------------------------


def test_disabled_overlay_config_short_circuits_without_walk():
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
        # overlay_enabled=false is a valid operator opt-out: by-name click
        # stays enabled but overlay_enabled_effective is False.
        h = UIActionHandler(
            response_queue=q,
            config={"ui_actions": {}, "click": {"overlay_enabled": False}},
        )

        walked = {"called": False}

        def _boom():
            walked["called"] = True
            raise AssertionError("must not walk when overlay disabled by config")

        with patch(f"{_MOD}._capture_click_foreground", side_effect=_boom):
            h.start_overlay_walk(
                scope="focused_window",
                overlay_session_id=9,
                paint_generation=0,
                trace_id="trace-cfg",
                request_id="req-walk-1",
            )

        assert walked["called"] is False
        assert q.put.call_count == 1
        payload = q.put.call_args[0][0]
        resp = StartOverlayWalkResponse.from_dict(payload)
        assert resp.outcome == "execution_failed"
        assert resp.reason == "disabled_by_config"
        # Generation + trace still echoed even on the short-circuit.
        assert resp.overlay_session_id == 9
        assert resp.paint_generation == 0
        assert resp.trace_id == "trace-cfg"


def test_overlay_disabled_when_by_name_click_disabled():
    # A bad Phase 1 key disables the whole feature, which makes
    # overlay_enabled_effective False too -- the overlay must short-circuit.
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

        with patch(f"{_MOD}._capture_click_foreground",
                   side_effect=AssertionError("must not walk")):
            h.start_overlay_walk(
                scope="focused_window",
                overlay_session_id=1,
                paint_generation=0,
                trace_id="t",
                request_id="req-walk-1",
            )

        assert q.put.call_count == 1
        resp = StartOverlayWalkResponse.from_dict(q.put.call_args[0][0])
        assert resp.outcome == "execution_failed"
        assert resp.reason == "disabled_by_config"


# ---------------------------------------------------------------------------
# Robustness: never raise; an unexpected error maps to outcome=error.
# ---------------------------------------------------------------------------


def test_unexpected_error_maps_to_error_outcome(handler):
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})

    boom_finder = MagicMock()
    boom_finder.overlay_walk.side_effect = RuntimeError("kaboom")
    handler._click_element_finder = boom_finder

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=2,
            paint_generation=7,
            trace_id="trace-err",
            request_id="req-walk-1",
        )

    resp = _last_response(handler)
    assert resp.status == "error"
    assert resp.outcome == "error"
    # Even on the crash path the generation + trace echo must survive.
    assert resp.overlay_session_id == 2
    assert resp.paint_generation == 7
    assert resp.trace_id == "trace-err"


def test_emits_exactly_one_response(handler):
    top = FakeArrayTopLevel([_el("Save")])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=1,
            paint_generation=0,
            trace_id="t",
            request_id="req-walk-1",
        )

    assert handler.response_queue.put.call_count == 1


def test_start_overlay_walk_in_handles_own_response_allowlist():
    from input_proc import _HANDLES_OWN_RESPONSE

    assert "start_overlay_walk" in _HANDLES_OWN_RESPONSE


# ---------------------------------------------------------------------------
# Owned-popup merge reaches the response summary (wh-n29v.75).
#
# overlay_walk now folds owned #32768 / UIA-Menu popup items into the numbered
# set, mirroring find(). This proves the popup item rides all the way out to the
# StartOverlayWalkResponse summary with a contiguous badge -- the asymmetry the
# slice closes (a menu item by-name "click <item>" can already target must also
# be numberable by "show numbers").
# ---------------------------------------------------------------------------


UIA_MENUITEM = uia_walker.UIA_MENUITEM
_POPUP_HWND = 2001


def _popup_walkresult(*names):
    """An owned-popup subtree WalkResult carrying interactive menu items.

    Each item gets its own vertically stacked rect (like a real menu):
    wh-overlay-nested-dupes made overlay geometry meaningful -- two fakes
    sharing one rect now read as one visual control and collapse to one
    badge, which no real pair of distinct menu items can be.
    """
    matches = [
        uia_walker.element_match_from_cached(
            FakeCachedElement(
                name=n,
                control_type=UIA_MENUITEM,
                localized_control_type="menu item",
                rect=FakeRect(10, 20 + 40 * (i - 1), 110, 50 + 40 * (i - 1)),
            ),
            display_number=i,
            source_window_hwnd=_POPUP_HWND,
        )
        for i, n in enumerate(names, start=1)
    ]
    return uia_walker.WalkResult(
        matches=matches,
        _keepalive_automation=object(),
        _keepalive_cache_request=object(),
        _keepalive_element_array=FakeElementArray([]),
        _keepalive_top_level_element=object(),
        deadline_truncated=False,
    )


def _make_finder_with_popup(top_level_element, popup_result):
    from ui.element_finder import ElementFinder

    return ElementFinder(
        dpi_resolver=lambda _m: 96.0,
        monitor_resolver=lambda _b: 0,
        walk_fn=_make_walk_fn(top_level_element),
        popup_walk_fn=lambda _h, **_k: [popup_result],
        window_enumerator=lambda: [],
    )


def test_owned_popup_item_reaches_response_summary_badged(handler):
    # Focused window has two interactive controls; an owned popup contributes
    # one menu item. The response summary must carry all three, contiguous 1..3.
    # Distinct side-by-side rects: same-rect fakes would read as one visual
    # control and collapse to one badge (wh-overlay-nested-dupes).
    top = FakeArrayTopLevel([
        _el("Save", rect=FakeRect(10, 20, 110, 70)),
        _el("Open", rect=FakeRect(120, 20, 220, 70)),
    ])
    handler._click_element_finder = _make_finder_with_popup(
        top, _popup_walkresult("Reload")
    )
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=2,
            trace_id="trace-popup",
            request_id="req-walk-1",
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "ok"
    assert resp.snapshot_summary is not None
    items = resp.snapshot_summary.items
    # The owned-popup menu item is numbered alongside the focused controls.
    assert [i.name for i in items] == ["Save", "Open", "Reload"]
    assert [i.display_number for i in items] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Walk deadline anchoring (reviewer_0 finding 38.2).
# ---------------------------------------------------------------------------


def test_walk_deadline_anchored_at_command_dequeue(handler):
    # reviewer_0 finding 38.2: the overlay walk deadline must anchor at the
    # dequeue instant the input_proc command reader captured (threaded in via
    # command_dequeue_monotonic), NOT at handler/walk entry. Charging from the
    # earliest reader instant folds the ~1s pre-handler reader stall into the
    # budget so the walk gives up before the Logic walk_in_flight timeout.
    # click_element already does this (wh-9f3t.73.1); start_overlay_walk mirrors
    # it. The deadline passed to ElementFinder.overlay_walk must equal
    # dequeue + walk_deadline_ms/1000.
    from ui.click_config import ClickConfig
    from ui.element_finder import OverlayWalkResult

    cfg = ClickConfig.from_raw({})
    handler._click_config = cfg

    finder = MagicMock()
    finder.overlay_walk.return_value = OverlayWalkResult(
        outcome="no_targets", reason=None, snapshot=None, summary=None,
    )
    handler._click_element_finder = finder

    dequeue = 1234.5
    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=1,
            paint_generation=0,
            trace_id="t",
            request_id="req-walk-1",
            command_dequeue_monotonic=dequeue,
        )

    assert finder.overlay_walk.call_count == 1
    passed_deadline = finder.overlay_walk.call_args.kwargs.get("deadline")
    assert passed_deadline is not None
    assert passed_deadline == dequeue + cfg.walk_deadline_ms / 1000.0


def test_walk_deadline_falls_back_to_handler_entry_without_dequeue(handler):
    # When no command_dequeue_monotonic is threaded in (a direct call, e.g. a
    # unit test or a caller that predates the plumbing), the deadline still
    # anchors -- at this handler's entry instant via time.monotonic() -- so the
    # walk is always bounded. Mirrors click_element's fallback.
    from ui.click_config import ClickConfig
    from ui.element_finder import OverlayWalkResult

    cfg = ClickConfig.from_raw({})
    handler._click_config = cfg

    finder = MagicMock()
    finder.overlay_walk.return_value = OverlayWalkResult(
        outcome="no_targets", reason=None, snapshot=None, summary=None,
    )
    handler._click_element_finder = finder

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()), \
         patch(f"{_MOD}.time.monotonic", return_value=500.0):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=1,
            paint_generation=0,
            trace_id="t",
            request_id="req-walk-1",
        )

    passed_deadline = finder.overlay_walk.call_args.kwargs.get("deadline")
    assert passed_deadline == 500.0 + cfg.walk_deadline_ms / 1000.0


def test_automation_unavailable_emits_distinct_reason():
    # Finding wh-n29v.74.1 (deepseek reviewer_2): when the overlay finder
    # short-circuits to None because the COM root could not be built
    # (create_automation() raises on a degraded / headless / locked-down host),
    # the overlay walk must emit reason="automation_unavailable" -- NOT
    # "disabled_by_config". Clicking IS enabled in config; the cause is the
    # machine, so the wording must not point the user at config.toml [click].
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
        # Default config: by-name click AND overlay both effectively enabled.
        h = UIActionHandler(response_queue=q, config={"ui_actions": {}})

        def _boom():
            raise OSError("UIAutomationCore unavailable")

        with patch("ui.uia_walker.create_automation", side_effect=_boom), \
             patch(f"{_MOD}._capture_click_foreground",
                   side_effect=AssertionError("must not walk when COM down")):
            h.start_overlay_walk(
                scope="focused_window",
                overlay_session_id=7,
                paint_generation=2,
                trace_id="trace-com",
                request_id="req-walk-1",
            )

        assert q.put.call_count == 1
        resp = StartOverlayWalkResponse.from_dict(q.put.call_args[0][0])
        assert resp.outcome == "execution_failed"
        assert resp.reason == "automation_unavailable"
        # Echo fields still carried on the short-circuit.
        assert resp.overlay_session_id == 7
        assert resp.paint_generation == 2
        assert resp.trace_id == "trace-com"
