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


def test_transient_com_error_maps_to_execution_failed_at_warning(handler, caplog):
    # wh-overlay-walk-theme-error: a Windows dark/light theme switch rebuilds
    # the shell window mid-walk; the walker's bounded stale-window retries can
    # all land inside the rebuild and re-raise COMError -2147220991. That is a
    # known transient class, not a handler crash: it must ride the NORMAL
    # execution_failed outcome (status="ok") and log at WARNING, because
    # ERROR-level records pop a Windows notification box
    # (ErrorNotificationHandler) for a blip that self-heals on the next
    # focus-hook re-walk.
    import logging

    try:
        from comtypes import COMError
        transient = COMError(
            -2147220991,
            "An event was unable to invoke any of the subscribers",
            (None, None, None, None, None),
        )
    except ImportError:
        transient = OSError("stale window during shell rebuild")

    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})

    boom_finder = MagicMock()
    boom_finder.overlay_walk.side_effect = transient
    handler._click_element_finder = boom_finder

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()), \
            caplog.at_level(logging.DEBUG):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=2,
            paint_generation=7,
            trace_id="trace-transient",
            request_id="req-walk-1",
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "execution_failed"
    assert resp.reason == "transient_com_error"
    assert resp.overlay_session_id == 2
    assert resp.paint_generation == 7
    assert resp.trace_id == "trace-transient"
    handler_records = [
        r for r in caplog.records if "start_overlay_walk" in r.getMessage()
    ]
    assert not [r for r in handler_records if r.levelno >= logging.ERROR]
    assert [r for r in handler_records if r.levelno == logging.WARNING]


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
    # dequeue + screen_read_walk_deadline_ms/1000 (the screen read's own bound
    # since wh-overlay-slow-uia-stale-badges.3).
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
    assert passed_deadline == dequeue + cfg.screen_read_walk_deadline_ms / 1000.0


def test_screen_read_deadline_uses_its_own_key_not_the_click_walk_bound(handler):
    """wh-overlay-slow-uia-stale-badges.3: the read reads its OWN key.

    ``[click] screen_read_timeout_ms`` (8000 here) bounds the screen read;
    ``walk_deadline_ms`` (2500 here) keeps bounding the by-name click walk
    only. The deadline handed to ``overlay_walk`` must therefore be
    dequeue + (8000 - 250)/1000 = dequeue + 7.75, not dequeue + 2.5.
    """
    from ui.click_config import ClickConfig
    from ui.element_finder import OverlayWalkResult

    cfg = ClickConfig.from_raw(
        {"screen_read_timeout_ms": 8000, "walk_deadline_ms": 2500}
    )
    assert cfg.invalid_key is None
    handler._click_config = cfg

    finder = MagicMock()
    finder.overlay_walk.return_value = OverlayWalkResult(
        outcome="no_targets", reason=None, snapshot=None, summary=None,
    )
    handler._click_element_finder = finder

    dequeue = 1000.0
    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=1,
            paint_generation=0,
            trace_id="t",
            request_id="req-walk-read-key",
            command_dequeue_monotonic=dequeue,
        )

    passed_deadline = finder.overlay_walk.call_args.kwargs.get("deadline")
    assert passed_deadline == dequeue + 7.75


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
    assert passed_deadline == 500.0 + cfg.screen_read_walk_deadline_ms / 1000.0


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


# ---------------------------------------------------------------------------
# The post-click settle re-read (wh-overlay-slow-uia-stale-badges.2).
#
# ``settle=True`` makes the handler read the window through
# ``ui.settle_detector.wait_for_settled_window`` instead of walking once, then
# compare the settled read against the snapshot Logic is still holding. The
# answer is the SAME snapshot id when nothing changed, which is how the Logic
# state machine learns to restore the numbers it already had. A changed screen
# can never produce that id by accident: every walk mints
# ``walk-<uuid4 hex>-<counter>`` with a per-run salt and a monotonic counter.
# ---------------------------------------------------------------------------


class FakeMutableTopLevel:
    """A fake top-level whose children can be swapped between walks."""

    def __init__(self, elements):
        self.elements = list(elements)
        self.walks = 0

    def FindAllBuildCache(self, _scope, _cond, _cache):
        self.walks += 1
        return FakeElementArray(list(self.elements))


def _held_snapshot_id(handler, foreground):
    """Walk once and return the id of the snapshot Logic would have pinned."""
    walk = handler._click_element_finder.overlay_walk(foreground)
    assert walk.outcome == "ok", walk.reason
    return walk.snapshot.snapshot_id


def test_settle_unchanged_screen_answers_with_the_held_snapshot_id(handler):
    """Acceptance criterion 3: nothing changed, so answer with the held id.

    The comparison is the ordered (control type id, accessible name, bounding
    rectangle) list ``signature_of`` builds -- the same list the detector uses
    to decide the window settled.
    """
    top = FakeMutableTopLevel([
        _el("Save", rect=FakeRect(10, 20, 110, 70)),
        _el("Cancel", rect=FakeRect(120, 20, 220, 70)),
    ])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    fg = _foreground()
    held = _held_snapshot_id(handler, fg)

    with patch(f"{_MOD}._capture_click_foreground", return_value=fg):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=3,
            trace_id="trace-settle-same",
            request_id="req-walk-1",
            settle=True,
            compare_snapshot_id=held,
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "ok"
    assert resp.snapshot_id == held
    # One walk built the held snapshot; the detector needs at least two more,
    # because one read cannot prove a window stopped changing.
    assert top.walks >= 3


def test_settle_changed_screen_answers_with_a_new_snapshot_id(handler):
    """Acceptance criterion 4: the content changed, so a new list is the answer.

    The new id is necessarily different from the held one -- ids are minted
    from a per-run salt and a monotonic counter and are never reused -- so the
    Logic side reads "changed" from the id alone.
    """
    top = FakeMutableTopLevel([
        _el("Save", rect=FakeRect(10, 20, 110, 70)),
        _el("Cancel", rect=FakeRect(120, 20, 220, 70)),
    ])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    fg = _foreground()
    held = _held_snapshot_id(handler, fg)

    # The click opened something: the window now shows a different control.
    top.elements = [
        _el("Save", rect=FakeRect(10, 20, 110, 70)),
        _el("Cancel", rect=FakeRect(120, 20, 220, 70)),
        _el("Overwrite?", rect=FakeRect(230, 20, 330, 70)),
    ]

    with patch(f"{_MOD}._capture_click_foreground", return_value=fg):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=3,
            trace_id="trace-settle-diff",
            request_id="req-walk-1",
            settle=True,
            compare_snapshot_id=held,
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "ok"
    assert resp.snapshot_id is not None
    assert resp.snapshot_id != held
    assert resp.snapshot_summary is not None
    assert {i.name for i in resp.snapshot_summary.items} == {
        "Save", "Cancel", "Overwrite?",
    }


def test_settle_refuses_to_reuse_a_held_list_from_another_window(handler):
    """wh-overlay-slow-uia-stale-badges.2.2.2.

    ``signature_of`` compares control type id, accessible name and bounding
    rectangle. It carries NO window identity, so two windows with the same
    layout compare as "unchanged". The ordinary way to reach that is a dialog
    closed and reopened: identical controls, a new window handle and a new
    creation time.

    Here the held list belongs to window 1000 and the settle read happens
    against window 2000. Answering with the held id would paint window 1000's
    badges over window 2000, and the machine's unchanged branch repaints
    WITHOUT re-pinning, so nothing would rebind the list to the window on
    screen. The answer must be the fresh read instead.
    """
    from ui.click_config import ClickConfig
    from ui.element_finder import ForegroundContext

    layout = [
        _el("Save", rect=FakeRect(10, 20, 110, 70)),
        _el("Cancel", rect=FakeRect(120, 20, 220, 70)),
    ]
    handler._click_element_finder = _make_finder(FakeMutableTopLevel(layout))
    handler._click_config = ClickConfig.from_raw({})

    window_a = _foreground()
    held = _held_snapshot_id(handler, window_a)

    # Same layout, different window: every identity field moves, which is what
    # a reopened dialog does.
    window_b = ForegroundContext(
        foreground_window=2000,
        foreground_pid=8765,
        foreground_process_name="notepad.exe",
        foreground_window_creation_time=1234,
        cursor_at_walk=(60, 45),
        cursor_monitor_id=0,
    )

    with patch(f"{_MOD}._capture_click_foreground", return_value=window_b):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=3,
            trace_id="trace-settle-other-window",
            request_id="req-walk-1",
            settle=True,
            compare_snapshot_id=held,
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "ok"
    assert resp.snapshot_id != held, (
        "the settle answer reused window 1000's list for a read of window 2000"
    )
    # The whole point of returning the fresh list: a later click against the
    # answer must not be refused for a foreground change. Asking the finder
    # the same question click_snapshot_item asks is what proves it.
    assert handler._click_element_finder.get_snapshot(
        resp.snapshot_id,
        current_foreground_window=window_b.foreground_window,
        current_foreground_pid=window_b.foreground_pid,
        current_foreground_process_name=window_b.foreground_process_name,
        current_foreground_window_creation_time=(
            window_b.foreground_window_creation_time
        ),
    ) is not None, (
        "the answered snapshot is already refused for the window it was read "
        "from, so the badges would paint and then every click would fail"
    )


def test_settle_uses_the_shared_detector_and_adds_no_second_mechanism(handler):
    """Acceptance criterion 2: the re-read goes through the shipped detector.

    Patching ``wait_for_settled_window`` is what proves it: a handler that
    grew its own settle loop would still pass the two tests above.
    """
    top = FakeMutableTopLevel([_el("Save", rect=FakeRect(10, 20, 110, 70))])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    fg = _foreground()
    held = _held_snapshot_id(handler, fg)

    from ui import settle_detector

    real = settle_detector.wait_for_settled_window
    calls = []

    def _spy(read, **kwargs):
        calls.append(kwargs)
        return real(read, **kwargs)

    with patch(f"{_MOD}._capture_click_foreground", return_value=fg), \
         patch.object(settle_detector, "wait_for_settled_window", _spy):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=3,
            trace_id="trace-settle-detector",
            request_id="req-walk-1",
            settle=True,
            compare_snapshot_id=held,
        )

    assert len(calls) == 1, "the settle read must go through the detector once"
    resp = _last_response(handler)
    assert resp.snapshot_id == held


def test_a_plain_walk_never_calls_the_detector(handler):
    """Criterion 7's Input-side half: with settle off nothing new runs.

    ``overlay_settle_after_click`` OFF means the machine never reaches the
    settling state, so no request ever carries settle=True. This proves the
    handler agrees: the shipped walk path is untouched.
    """
    top = FakeMutableTopLevel([_el("Save", rect=FakeRect(10, 20, 110, 70))])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})

    from ui import settle_detector

    calls = []

    def _spy(read, **kwargs):  # pragma: no cover - must never run
        calls.append(kwargs)
        raise AssertionError("the plain walk path called the settle detector")

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()), \
         patch.object(settle_detector, "wait_for_settled_window", _spy):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=3,
            trace_id="trace-plain",
            request_id="req-walk-1",
        )

    assert calls == []
    resp = _last_response(handler)
    assert resp.outcome == "ok"
    assert top.walks == 1


def test_the_settle_read_keeps_the_detector_own_maximum(handler):
    """Acceptance criterion 6, the Input half of the double bound.

    The handler must not pass its own ``max_ms``. David set 1500 ms on the
    bead and the detector's module constant is the single place it lives, so
    a handler that supplied its own number would put a second, unreviewed
    bound in the path and the config one would stop meaning anything.
    """
    top = FakeMutableTopLevel([_el("Save", rect=FakeRect(10, 20, 110, 70))])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    fg = _foreground()
    held = _held_snapshot_id(handler, fg)

    from ui import settle_detector

    real = settle_detector.wait_for_settled_window
    seen = []

    def _spy(read, **kwargs):
        seen.append(kwargs)
        return real(read, **kwargs)

    with patch(f"{_MOD}._capture_click_foreground", return_value=fg), \
         patch.object(settle_detector, "wait_for_settled_window", _spy):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=3,
            trace_id="trace-settle-max",
            request_id="req-walk-1",
            settle=True,
            compare_snapshot_id=held,
        )

    assert len(seen) == 1
    assert "max_ms" not in seen[0]
    assert settle_detector.DEFAULT_SETTLE_MAX_MS == 1500


def test_a_window_that_never_settles_answers_with_the_last_read(handler):
    """Acceptance criterion 6: an unsettled window still produces numbers.

    The detector returns its last completed read with ``settled=False`` when
    the maximum expires. That read is a real, complete picture of one moment,
    so it is painted -- the alternative is closing the overlay on exactly the
    slow machines this whole bead exists for.
    """
    top = FakeMutableTopLevel([_el("Save", rect=FakeRect(10, 20, 110, 70))])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    fg = _foreground()
    held = _held_snapshot_id(handler, fg)

    from ui import settle_detector

    real = settle_detector.wait_for_settled_window

    def _never_settles(read, **kwargs):
        # A clock that is already past the deadline on the first check: the
        # detector takes its one mandatory read and returns settled=False.
        ticks = iter([0.0] + [10_000.0] * 64)
        return real(read, events_between=None, now_ms=lambda: next(ticks))

    with patch(f"{_MOD}._capture_click_foreground", return_value=fg), \
         patch.object(settle_detector, "wait_for_settled_window",
                      _never_settles):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=3,
            trace_id="trace-never-settles",
            request_id="req-walk-1",
            settle=True,
            compare_snapshot_id=held,
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "ok"
    assert resp.snapshot_summary is not None
    assert [i.name for i in resp.snapshot_summary.items] == ["Save"]


def test_a_vanished_held_snapshot_answers_with_the_fresh_read(handler):
    """The held id is answered with ONLY after an equal comparison.

    Its store entry can be gone by the time the settle finishes -- the 30 s
    TTL expired, or it was unpinned and evicted. There is then nothing to
    compare against, and answering with the id anyway would name a snapshot
    the Input process is no longer holding, so a later "click 3" would find
    no list. The fresh read is the answer instead.
    """
    top = FakeMutableTopLevel([_el("Save", rect=FakeRect(10, 20, 110, 70))])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    fg = _foreground()
    held = _held_snapshot_id(handler, fg)
    # The screen is UNCHANGED, so only the missing entry can decide this.
    # ``invalidate`` is the shipped way the store loses entries; TTL expiry
    # and LRU eviction reach the same place through ``_drop``.
    handler._click_element_finder.invalidate()
    assert handler._click_element_finder.get_snapshot(held) is None

    with patch(f"{_MOD}._capture_click_foreground", return_value=fg):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=3,
            trace_id="trace-held-gone",
            request_id="req-walk-1",
            settle=True,
            compare_snapshot_id=held,
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.snapshot_id is not None
    assert resp.snapshot_id != held
    assert resp.snapshot_summary is not None


def test_a_settle_with_no_compare_id_answers_with_the_fresh_read(handler):
    """An empty compare id is not a licence to report "unchanged".

    Nothing in the shipped path sends one, so this pins the degrade rather
    than a live case: with no comparison target the settled read is the
    answer, exactly as a plain walk would be.
    """
    top = FakeMutableTopLevel([_el("Save", rect=FakeRect(10, 20, 110, 70))])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=3,
            trace_id="trace-no-compare",
            request_id="req-walk-1",
            settle=True,
            compare_snapshot_id="",
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "ok"
    assert resp.snapshot_id
    assert resp.snapshot_summary is not None


def test_the_held_id_is_answered_with_the_held_snapshot_own_summary(handler):
    """The held id and the summary beside it must name the SAME snapshot.

    A ``WalkSnapshotSummary`` names the snapshot it was projected from, and
    ``StartOverlayWalkResponse`` refuses a response whose
    ``snapshot_summary.snapshot_id`` differs from its top-level
    ``snapshot_id`` (shared/start_overlay_walk.py:266) -- Logic keys the
    retained summary by the top-level id. So answering "unchanged" with the
    held id but the fresh walk's summary would be rejected at the process
    boundary and the whole response lost.

    Note what this does NOT rest on: the item ids are identical either way.
    Measured -- an item id is "<walker source>-<index>" and neither part
    carries a snapshot id, so two walks of an unchanged window mint the same
    ids. The snapshot_id agreement is the whole of the requirement.
    """
    top = FakeMutableTopLevel([
        _el("Save", rect=FakeRect(10, 20, 110, 70)),
        _el("Cancel", rect=FakeRect(120, 20, 220, 70)),
    ])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    fg = _foreground()
    held = _held_snapshot_id(handler, fg)

    with patch(f"{_MOD}._capture_click_foreground", return_value=fg):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=3,
            trace_id="trace-held-summary",
            request_id="req-walk-1",
            settle=True,
            compare_snapshot_id=held,
        )

    resp = _last_response(handler)
    assert resp.snapshot_id == held
    assert resp.snapshot_summary is not None
    assert resp.snapshot_summary.snapshot_id == held
    # And the response really does survive the schema the boundary applies.
    StartOverlayWalkResponse.from_dict(resp.to_dict())


def test_a_failed_second_read_answers_with_the_first_completed_read(handler):
    """A read that produces no list does not throw away the one that did.

    The detector's own contract is never to paint nothing when a read has
    completed (decision point 3). A settle whose LAST read fails still has an
    earlier post-click read in hand, and that read is no worse than the single
    walk this path replaced -- so it is the answer, and the comparison against
    the held snapshot still runs over it.
    """
    top = FakeMutableTopLevel([_el("Save", rect=FakeRect(10, 20, 110, 70))])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    fg = _foreground()
    held = _held_snapshot_id(handler, fg)

    finder = handler._click_element_finder
    real_walk = finder.overlay_walk
    calls = {"n": 0}

    def _fail_after_the_first(foreground, **kwargs):
        calls["n"] += 1
        result = real_walk(foreground, **kwargs)
        if calls["n"] == 1:
            return result
        # Every later read reports the deadline-truncation failure, which is
        # the shipped no-list outcome (overlay_walk returns snapshot=None).
        from ui.element_finder import OverlayWalkResult
        return OverlayWalkResult(
            outcome="execution_failed",
            reason="walk_deadline_exceeded",
            snapshot=None,
            summary=None,
        )

    with patch(f"{_MOD}._capture_click_foreground", return_value=fg), \
         patch.object(finder, "overlay_walk", _fail_after_the_first):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=3,
            trace_id="trace-second-read-failed",
            request_id="req-walk-1",
            settle=True,
            compare_snapshot_id=held,
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "ok", resp.reason
    assert resp.snapshot_summary is not None
    assert [i.name for i in resp.snapshot_summary.items] == ["Save"]


def test_a_settle_with_no_completed_read_reports_the_failure(handler):
    """The boundary of the rule above: nothing completed, so nothing to paint.

    With no list in hand there is no honest answer but the failure, and the
    handler's shipped execution_failed branch produces it.
    """
    top = FakeMutableTopLevel([_el("Save", rect=FakeRect(10, 20, 110, 70))])
    handler._click_element_finder = _make_finder(top)
    from ui.click_config import ClickConfig
    handler._click_config = ClickConfig.from_raw({})
    fg = _foreground()
    held = _held_snapshot_id(handler, fg)

    finder = handler._click_element_finder

    def _always_fails(foreground, **kwargs):
        from ui.element_finder import OverlayWalkResult
        return OverlayWalkResult(
            outcome="execution_failed",
            reason="walk_deadline_exceeded",
            snapshot=None,
            summary=None,
        )

    with patch(f"{_MOD}._capture_click_foreground", return_value=fg), \
         patch.object(finder, "overlay_walk", _always_fails):
        handler.start_overlay_walk(
            scope="focused_window",
            overlay_session_id=5,
            paint_generation=3,
            trace_id="trace-no-read-at-all",
            request_id="req-walk-1",
            settle=True,
            compare_snapshot_id=held,
        )

    resp = _last_response(handler)
    assert resp.outcome == "execution_failed"
    assert resp.reason == "walk_deadline_exceeded"
    assert resp.snapshot_id is None
    assert resp.snapshot_summary is None
