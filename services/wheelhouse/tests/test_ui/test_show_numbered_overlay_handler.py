"""End-to-end Input-side tests for UIActionHandler.show_numbered_overlay (wh-n29v.83).

``show_numbered_overlay`` is the "re-paint an EXISTING snapshot" overlay
request: unlike ``start_overlay_walk`` (which walks the focused window from
scratch), it LOOKS UP a snapshot the multi-snapshot store already holds via
``ElementFinder.get_snapshot``, builds a fresh display ``WalkSnapshotSummary``,
optionally filters it to an ``item_id_filter`` and renumbers the kept items
1..K in reading order, and emits exactly one ``ShowNumberedOverlayResponse``
carrying the snapshot id + summary and the echoed
``overlay_session_id`` / ``paint_generation`` / ``trace_id``.

Covered here (the design v4 line-545 test matrix -- every outcome literal
exercised, generation echoed, filter+renumber, from_dict round-trip):
  * ok: a stored snapshot is found, the summary is built and (when filtered)
    renumbered 1..K, status=ok, summary.snapshot_id == top-level snapshot_id.
  * snapshot_expired: get_snapshot returns None (stale id / TTL-swept /
    LRU-evicted / foreground-identity mismatch) -> outcome=snapshot_expired,
    status=ok, snapshot_id=None, snapshot_summary=None, reason=stale_snapshot_id.
  * no_targets: a found snapshot with zero items, or an item_id_filter that
    excludes everything -> outcome=no_targets carrying an empty-items summary
    or None (never a populated summary).
  * execution_failed: disabled-overlay-config and automation-unavailable
    short-circuits, matching start_overlay_walk's two reason tags.
  * error: an unexpected exception maps to status=error / outcome=error
    (never raises).
  * item_id_filter restricts the kept set AND renumbers it contiguously.
  * generation/trace echo: overlay_session_id, paint_generation, trace_id are
    echoed verbatim on every response.
  * every emitted payload passes ShowNumberedOverlayResponse.from_dict().
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.wheelhouse.shared.show_numbered_overlay import (
    ShowNumberedOverlayResponse,
)
from ui.element_types import ElementMatch, WalkSnapshot


_MOD = "ui.ui_action_handler"


# ---------------------------------------------------------------------------
# Snapshot construction helpers.
# ---------------------------------------------------------------------------


def _match(item_id, display_number, name, *, role="button"):
    return ElementMatch(
        item_id=item_id,
        display_number=display_number,
        name=name,
        role=role,
        bounds=(10 * display_number, 20, 110, 70),
        monitor_id=0,
        score=1.0,
        is_eligible=True,
        source="uia",
        invoke_supported=True,
        is_enabled=True,
        control_ref=object(),
        control_type_id=50000,
        source_window_hwnd=0,
    )


def _snapshot(snapshot_id, matches):
    return WalkSnapshot(
        snapshot_id=snapshot_id,
        matches=matches,
        created_at_monotonic=123.0,
        foreground_window=1000,
        foreground_pid=4321,
        foreground_process_name="notepad.exe",
        foreground_window_creation_time=99,
        cursor_at_walk=(60, 45),
        cursor_monitor_id=0,
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


def _last_response(handler) -> ShowNumberedOverlayResponse:
    """Assert exactly one response was enqueued, parse + validate it."""
    assert handler.response_queue.put.call_count == 1
    payload = handler.response_queue.put.call_args[0][0]
    assert payload["action"] == "show_numbered_overlay"
    assert payload["request_id"] == "req-show-1"
    # Round-trip through the shipped validator -- every emitted payload MUST
    # parse cleanly (no ShowNumberedOverlayResponseSchemaError).
    return ShowNumberedOverlayResponse.from_dict(payload)


def _enable_overlay(handler, finder):
    from ui.click_config import ClickConfig

    handler._click_element_finder = finder
    handler._click_config = ClickConfig.from_raw({})


# ---------------------------------------------------------------------------
# ok: a stored snapshot is found and painted.
# ---------------------------------------------------------------------------


def test_ok_returns_summary_with_snapshot_id(handler):
    snap = _snapshot("snap-A", [
        _match("it-1", 1, "Save"),
        _match("it-2", 2, "Open"),
        _match("it-3", 3, "Cancel"),
    ])
    finder = MagicMock()
    finder.get_snapshot.return_value = snap
    _enable_overlay(handler, finder)

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            overlay_session_id=5,
            paint_generation=2,
            trace_id="trace-ok",
            request_id="req-show-1",
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "ok"
    assert resp.snapshot_id == "snap-A"
    assert resp.snapshot_summary is not None
    # Cross-field rule (c): summary names the same snapshot.
    assert resp.snapshot_summary.snapshot_id == "snap-A"
    items = resp.snapshot_summary.items
    assert [i.name for i in items] == ["Save", "Open", "Cancel"]
    assert [i.display_number for i in items] == [1, 2, 3]
    # Generation + trace echoed verbatim.
    assert resp.overlay_session_id == 5
    assert resp.paint_generation == 2
    assert resp.trace_id == "trace-ok"


def test_ok_passes_captured_foreground_identity_to_get_snapshot(handler):
    snap = _snapshot("snap-A", [_match("it-1", 1, "Save")])
    finder = MagicMock()
    finder.get_snapshot.return_value = snap
    _enable_overlay(handler, finder)

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            overlay_session_id=1,
            paint_generation=0,
            trace_id="t",
            request_id="req-show-1",
        )

    # The captured foreground identity is threaded into get_snapshot so a
    # foreground change invalidates the snapshot (the snapshot_expired signal).
    assert finder.get_snapshot.call_count == 1
    kwargs = finder.get_snapshot.call_args.kwargs
    assert kwargs["current_foreground_window"] == 1000
    assert kwargs["current_foreground_pid"] == 4321
    assert kwargs["current_foreground_process_name"] == "notepad.exe"
    assert kwargs["current_foreground_window_creation_time"] == 99


# ---------------------------------------------------------------------------
# snapshot_expired: get_snapshot returns None.
# ---------------------------------------------------------------------------


def test_snapshot_expired_when_get_snapshot_returns_none(handler):
    finder = MagicMock()
    finder.get_snapshot.return_value = None
    _enable_overlay(handler, finder)

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="gone",
            overlay_session_id=4,
            paint_generation=1,
            trace_id="trace-exp",
            request_id="req-show-1",
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "snapshot_expired"
    # Cross-field rule (d): a failure outcome carries no snapshot.
    assert resp.snapshot_id is None
    assert resp.snapshot_summary is None
    assert resp.reason == "stale_snapshot_id"
    assert resp.overlay_session_id == 4
    assert resp.paint_generation == 1
    assert resp.trace_id == "trace-exp"


# ---------------------------------------------------------------------------
# no_targets: a found snapshot with zero items, or filter empties the set.
# ---------------------------------------------------------------------------


def test_no_targets_when_snapshot_has_no_items(handler):
    snap = _snapshot("snap-empty", [])
    finder = MagicMock()
    finder.get_snapshot.return_value = snap
    _enable_overlay(handler, finder)

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="snap-empty",
            overlay_session_id=1,
            paint_generation=0,
            trace_id="trace-nt",
            request_id="req-show-1",
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "no_targets"
    # The handler ALWAYS emits a non-None empty-items summary on no_targets
    # (it passes snapshot_summary=summary from filter_and_renumber_summary,
    # which never returns None). Assert that unconditionally so a future
    # refactor that drops the summary on no_targets is caught -- a conditional
    # `if summary is not None` guard would pass vacuously (wh-n29v.86.2).
    assert resp.snapshot_summary is not None
    assert resp.snapshot_summary.items == []
    # Cross-field rule (c): the summary names the same snapshot as the
    # top-level snapshot_id.
    assert resp.snapshot_summary.snapshot_id == resp.snapshot_id


def test_no_targets_when_filter_excludes_everything(handler):
    snap = _snapshot("snap-A", [
        _match("it-1", 1, "Save"),
        _match("it-2", 2, "Open"),
    ])
    finder = MagicMock()
    finder.get_snapshot.return_value = snap
    _enable_overlay(handler, finder)

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            item_id_filter=["does-not-exist"],
            overlay_session_id=1,
            paint_generation=0,
            trace_id="trace-nt2",
            request_id="req-show-1",
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "no_targets"
    # Same unconditional assertion as the empty-snapshot case: a filter that
    # excludes every item still yields a non-None empty-items summary naming
    # the same snapshot (wh-n29v.86.2).
    assert resp.snapshot_summary is not None
    assert resp.snapshot_summary.items == []
    assert resp.snapshot_summary.snapshot_id == resp.snapshot_id


# ---------------------------------------------------------------------------
# item_id_filter restricts the kept set AND renumbers contiguously.
# ---------------------------------------------------------------------------


def test_item_id_filter_restricts_and_renumbers(handler):
    snap = _snapshot("snap-A", [
        _match("it-1", 1, "Save"),
        _match("it-2", 2, "Open"),
        _match("it-3", 3, "Cancel"),
        _match("it-4", 4, "Help"),
    ])
    finder = MagicMock()
    finder.get_snapshot.return_value = snap
    _enable_overlay(handler, finder)

    # Keep items 2 and 4 (out of order in the filter); the kept set must follow
    # the snapshot's reading order and renumber 1..K contiguously.
    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            item_id_filter=["it-4", "it-2"],
            overlay_session_id=2,
            paint_generation=3,
            trace_id="trace-filter",
            request_id="req-show-1",
        )

    resp = _last_response(handler)
    assert resp.status == "ok"
    assert resp.outcome == "ok"
    assert resp.snapshot_summary is not None
    assert resp.snapshot_summary.snapshot_id == "snap-A"
    items = resp.snapshot_summary.items
    # Only it-2 (Open) and it-4 (Help) survive, in reading order, renumbered 1..2.
    assert [i.item_id for i in items] == ["it-2", "it-4"]
    assert [i.name for i in items] == ["Open", "Help"]
    assert [i.display_number for i in items] == [1, 2]
    assert resp.overlay_session_id == 2
    assert resp.paint_generation == 3
    assert resp.trace_id == "trace-filter"


def test_no_filter_renumbers_full_set_one_to_k(handler):
    # A snapshot whose display_numbers are non-contiguous (e.g. carried over
    # from a richer walk). With no filter the full set is renumbered 1..K.
    snap = _snapshot("snap-A", [
        _match("it-1", 7, "Save"),
        _match("it-2", 9, "Open"),
    ])
    finder = MagicMock()
    finder.get_snapshot.return_value = snap
    _enable_overlay(handler, finder)

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            overlay_session_id=1,
            paint_generation=0,
            trace_id="t",
            request_id="req-show-1",
        )

    resp = _last_response(handler)
    assert resp.outcome == "ok"
    assert resp.snapshot_summary is not None
    assert [i.display_number for i in resp.snapshot_summary.items] == [1, 2]


# ---------------------------------------------------------------------------
# execution_failed: disabled-overlay-config and automation-unavailable.
# ---------------------------------------------------------------------------


def test_disabled_overlay_config_short_circuits_without_lookup():
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
            config={"ui_actions": {}, "click": {"overlay_enabled": False}},
        )

        # click is enabled here (only the overlay is disabled), so the shared
        # _get_click_element_finder DOES build the COM root. Mock
        # create_automation so the build succeeds deterministically: this test
        # exercises the overlay-disabled gate with COM AVAILABLE, isolating
        # disabled_by_config from the host's real UIAutomation state. Without
        # the mock the result depends on whether the host can build COM -- on a
        # degraded host create_automation raises, the sentinel is set, and the
        # handler emits automation_unavailable (the deliberate wh-n29v.74.1
        # precedence), so the disabled_by_config assertion would fail. That
        # COM-down-while-disabled case is covered separately below.
        with patch("ui.uia_walker.create_automation",
                   return_value=MagicMock()), \
             patch(f"{_MOD}._capture_click_foreground",
                   side_effect=AssertionError("must not capture when disabled")):
            h.show_numbered_overlay(
                snapshot_id="snap-A",
                overlay_session_id=9,
                paint_generation=0,
                trace_id="trace-cfg",
                request_id="req-show-1",
            )

        assert q.put.call_count == 1
        resp = ShowNumberedOverlayResponse.from_dict(q.put.call_args[0][0])
        assert resp.status == "ok"
        assert resp.outcome == "execution_failed"
        assert resp.reason == "disabled_by_config"
        assert resp.snapshot_id is None
        assert resp.snapshot_summary is None
        assert resp.overlay_session_id == 9
        assert resp.paint_generation == 0
        assert resp.trace_id == "trace-cfg"


def test_disabled_overlay_with_com_unavailable_reports_automation_unavailable():
    # Overlay disabled in config AND the host cannot build COM (click stays
    # enabled). The handler reports automation_unavailable, NOT
    # disabled_by_config -- the deliberate wh-n29v.74.1 precedence mirrored from
    # start_overlay_walk: when COM is genuinely unavailable the machine is the
    # cause, and pointing the user at config.toml would be misleading. This
    # locks in the behaviour a degraded host actually produces so the
    # disabled-overlay reason is no longer host-dependent by accident.
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
            config={"ui_actions": {}, "click": {"overlay_enabled": False}},
        )

        def _boom():
            raise OSError("UIAutomationCore unavailable")

        with patch("ui.uia_walker.create_automation", side_effect=_boom), \
             patch(f"{_MOD}._capture_click_foreground",
                   side_effect=AssertionError("must not capture when COM down")):
            h.show_numbered_overlay(
                snapshot_id="snap-A",
                overlay_session_id=9,
                paint_generation=0,
                trace_id="trace-cfg",
                request_id="req-show-1",
            )

        assert q.put.call_count == 1
        resp = ShowNumberedOverlayResponse.from_dict(q.put.call_args[0][0])
        assert resp.status == "ok"
        assert resp.outcome == "execution_failed"
        assert resp.reason == "automation_unavailable"
        assert resp.snapshot_id is None
        assert resp.snapshot_summary is None


def test_overlay_disabled_when_by_name_click_disabled():
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
                   side_effect=AssertionError("must not capture")):
            h.show_numbered_overlay(
                snapshot_id="snap-A",
                overlay_session_id=1,
                paint_generation=0,
                trace_id="t",
                request_id="req-show-1",
            )

        assert q.put.call_count == 1
        resp = ShowNumberedOverlayResponse.from_dict(q.put.call_args[0][0])
        assert resp.outcome == "execution_failed"
        assert resp.reason == "disabled_by_config"


def test_automation_unavailable_emits_distinct_reason():
    # When the overlay finder short-circuits to None because the COM root could
    # not be built (the _AUTOMATION_UNAVAILABLE sentinel), the handler must emit
    # reason="automation_unavailable" -- NOT "disabled_by_config". Clicking IS
    # enabled in config; the cause is the machine.
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

        def _boom():
            raise OSError("UIAutomationCore unavailable")

        with patch("ui.uia_walker.create_automation", side_effect=_boom), \
             patch(f"{_MOD}._capture_click_foreground",
                   side_effect=AssertionError("must not capture when COM down")):
            h.show_numbered_overlay(
                snapshot_id="snap-A",
                overlay_session_id=7,
                paint_generation=2,
                trace_id="trace-com",
                request_id="req-show-1",
            )

        assert q.put.call_count == 1
        resp = ShowNumberedOverlayResponse.from_dict(q.put.call_args[0][0])
        assert resp.status == "ok"
        assert resp.outcome == "execution_failed"
        assert resp.reason == "automation_unavailable"
        assert resp.snapshot_id is None
        assert resp.snapshot_summary is None
        assert resp.overlay_session_id == 7
        assert resp.paint_generation == 2
        assert resp.trace_id == "trace-com"


# ---------------------------------------------------------------------------
# invalid_request: a malformed Logic message is rejected before any lookup with
# a schema-valid status=error / outcome=error response, and the echoed scalars
# are coerced to schema-safe primitives so the response still passes from_dict
# (wh-n29v.85.2). Mirrors the pin_snapshot / unpin_snapshot IPC-field guard.
# ---------------------------------------------------------------------------


def test_malformed_paint_generation_rejected_as_invalid_request(handler):
    # paint_generation=True is a bool, not a valid non-bool int. The handler
    # rejects before any lookup (foreground capture must not run) and coerces
    # the echo scalar to a real int so the error response passes from_dict.
    with patch(f"{_MOD}._capture_click_foreground",
               side_effect=AssertionError("must not capture on invalid request")):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            overlay_session_id=5,
            paint_generation=True,
            trace_id="trace-bad",
            request_id="req-show-1",
        )
    resp = _last_response(handler)
    assert resp.status == "error"
    assert resp.outcome == "error"
    assert resp.reason == "invalid_request"
    assert resp.snapshot_id is None
    assert resp.snapshot_summary is None
    assert resp.paint_generation == 0
    assert isinstance(resp.paint_generation, int) and not isinstance(
        resp.paint_generation, bool
    )
    assert resp.overlay_session_id == 5
    assert resp.trace_id == "trace-bad"


def test_malformed_trace_id_rejected_and_coerced(handler):
    # trace_id=None violates the schema's str requirement. The handler rejects
    # with invalid_request AND coerces trace_id to "" so the error response
    # itself passes from_dict (otherwise Logic would log-drop it).
    with patch(f"{_MOD}._capture_click_foreground",
               side_effect=AssertionError("must not capture on invalid request")):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            overlay_session_id=5,
            paint_generation=0,
            trace_id=None,
            request_id="req-show-1",
        )
    resp = _last_response(handler)
    assert resp.status == "error"
    assert resp.outcome == "error"
    assert resp.reason == "invalid_request"
    assert resp.trace_id == ""


def test_malformed_item_id_filter_rejected_as_invalid_request(handler):
    # item_id_filter must be None or a list of str. A non-list is rejected
    # before the lookup -- a bad filter would also break
    # filter_and_renumber_summary's set() membership build.
    with patch(f"{_MOD}._capture_click_foreground",
               side_effect=AssertionError("must not capture on invalid request")):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            item_id_filter="not-a-list",
            overlay_session_id=5,
            paint_generation=0,
            trace_id="t",
            request_id="req-show-1",
        )
    resp = _last_response(handler)
    assert resp.status == "error"
    assert resp.outcome == "error"
    assert resp.reason == "invalid_request"
    assert resp.snapshot_id is None
    assert resp.snapshot_summary is None


def test_malformed_item_id_filter_list_with_non_str_elements_rejected(handler):
    # item_id_filter must be None or a list of *str*. A list carrying a non-str
    # element exercises the `all(isinstance(x, str) for x in item_id_filter)`
    # clause of the invalid_request guard, which is distinct from the
    # `isinstance(item_id_filter, list)` clause the "not-a-list" test covers.
    # Without the guard such a list would reach filter_and_renumber_summary,
    # where set([123, "valid"]) silently keeps the int (which never matches any
    # str item_id) and produces a confusing no_targets instead of a clean
    # invalid_request reject (wh-n29v.86.1). The AssertionError side_effect
    # proves the guard fired BEFORE any snapshot lookup: had control reached
    # _capture_click_foreground, the never-raise handler would map the raised
    # AssertionError to reason="unexpected_error", failing the assertion below.
    with patch(f"{_MOD}._capture_click_foreground",
               side_effect=AssertionError("must not capture on invalid request")):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            item_id_filter=[123, "valid"],
            overlay_session_id=5,
            paint_generation=0,
            trace_id="t",
            request_id="req-show-1",
        )
    resp = _last_response(handler)
    assert resp.status == "error"
    assert resp.outcome == "error"
    assert resp.reason == "invalid_request"
    assert resp.snapshot_id is None
    assert resp.snapshot_summary is None


# ---------------------------------------------------------------------------
# error: an unexpected exception maps to outcome=error (never raises).
# ---------------------------------------------------------------------------


def test_unexpected_error_maps_to_error_outcome(handler):
    boom_finder = MagicMock()
    boom_finder.get_snapshot.side_effect = RuntimeError("kaboom")
    _enable_overlay(handler, boom_finder)

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            overlay_session_id=2,
            paint_generation=7,
            trace_id="trace-err",
            request_id="req-show-1",
        )

    resp = _last_response(handler)
    assert resp.status == "error"
    assert resp.outcome == "error"
    # Cross-field rule (d): error carries no snapshot.
    assert resp.snapshot_id is None
    assert resp.snapshot_summary is None
    # Even on the crash path the generation + trace echo must survive.
    assert resp.overlay_session_id == 2
    assert resp.paint_generation == 7
    assert resp.trace_id == "trace-err"


# ---------------------------------------------------------------------------
# Exactly-one-response contract and the _HANDLES_OWN_RESPONSE allowlist.
# ---------------------------------------------------------------------------


def test_emits_exactly_one_response(handler):
    snap = _snapshot("snap-A", [_match("it-1", 1, "Save")])
    finder = MagicMock()
    finder.get_snapshot.return_value = snap
    _enable_overlay(handler, finder)

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            overlay_session_id=1,
            paint_generation=0,
            trace_id="t",
            request_id="req-show-1",
        )

    assert handler.response_queue.put.call_count == 1


def test_real_handler_never_emits_not_implemented(handler):
    snap = _snapshot("snap-A", [_match("it-1", 1, "Save")])
    finder = MagicMock()
    finder.get_snapshot.return_value = snap
    _enable_overlay(handler, finder)

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            overlay_session_id=1,
            paint_generation=0,
            trace_id="t",
            request_id="req-show-1",
        )

    payload = handler.response_queue.put.call_args[0][0]
    assert payload["status"] != "not_implemented"


def test_show_numbered_overlay_in_handles_own_response_allowlist():
    from input_proc import _HANDLES_OWN_RESPONSE

    assert "show_numbered_overlay" in _HANDLES_OWN_RESPONSE


# ---------------------------------------------------------------------------
# Auto-open display collapse (reviewer_0 finding wh-overlay-nested-dupes.1.2):
# the display path collapses coincident container+inner pairs the same way
# overlay_walk does, so the ambiguous auto-open never paints two badges on
# the same pixels.
# ---------------------------------------------------------------------------


def _match_at(item_id, display_number, name, bounds):
    return ElementMatch(
        item_id=item_id,
        display_number=display_number,
        name=name,
        role="button",
        bounds=bounds,
        monitor_id=0,
        score=1.0,
        is_eligible=True,
        source="uia",
        invoke_supported=True,
        is_enabled=True,
        control_ref=object(),
        control_type_id=50000,
        source_window_hwnd=0,
    )


def test_filter_collapses_coincident_wrapper_and_inner(handler):
    # The live-confirmed Brave shape: wrapper + link, identical name, identical
    # rect -- exactly the pair find() reports as ambiguous. The auto-open
    # display must paint ONE badge on those pixels, not two stacked ones.
    snap = _snapshot("snap-A", [
        _match_at("it-1", 1, "Open item", (0, 0, 200, 20)),
        _match_at("it-2", 2, "Open item", (0, 0, 200, 20)),
        _match_at("it-3", 3, "Save", (300, 0, 100, 20)),
    ])
    finder = MagicMock()
    finder.get_snapshot.return_value = snap
    _enable_overlay(handler, finder)

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            overlay_session_id=5,
            paint_generation=2,
            trace_id="trace-collapse",
            request_id="req-show-1",
            item_id_filter=["it-1", "it-2"],
        )

    resp = _last_response(handler)
    assert resp.outcome == "ok"
    items = resp.snapshot_summary.items
    # The inner (later, more specific) element keeps the badge; the survivor
    # still resolves against the stored snapshot for click_snapshot_item.
    assert [i.item_id for i in items] == ["it-2"]
    assert [i.display_number for i in items] == [1]


def test_unfiltered_repaint_also_collapses(handler):
    snap = _snapshot("snap-A", [
        _match_at("it-1", 1, "Open item", (0, 0, 200, 20)),
        _match_at("it-2", 2, "Open item", (0, 0, 200, 20)),
        _match_at("it-3", 3, "Save", (300, 0, 100, 20)),
    ])
    finder = MagicMock()
    finder.get_snapshot.return_value = snap
    _enable_overlay(handler, finder)

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            overlay_session_id=5,
            paint_generation=2,
            trace_id="trace-collapse-2",
            request_id="req-show-1",
        )

    resp = _last_response(handler)
    assert resp.outcome == "ok"
    items = resp.snapshot_summary.items
    assert [i.item_id for i in items] == ["it-2", "it-3"]
    assert [i.display_number for i in items] == [1, 2]
    assert [i.name for i in items] == ["Open item", "Save"]


def test_filter_keeps_distinct_rect_finalists(handler):
    # Distinct-rect finalists (a real two-buttons-same-name ambiguity) are all
    # kept -- the collapse only merges (near-)coincident geometry.
    snap = _snapshot("snap-A", [
        _match_at("it-1", 1, "Submit", (0, 0, 100, 20)),
        _match_at("it-2", 2, "Submit", (200, 0, 100, 20)),
    ])
    finder = MagicMock()
    finder.get_snapshot.return_value = snap
    _enable_overlay(handler, finder)

    with patch(f"{_MOD}._capture_click_foreground", return_value=_foreground()):
        handler.show_numbered_overlay(
            snapshot_id="snap-A",
            overlay_session_id=1,
            paint_generation=0,
            trace_id="t",
            request_id="req-show-1",
            item_id_filter=["it-1", "it-2"],
        )

    resp = _last_response(handler)
    items = resp.snapshot_summary.items
    assert [i.item_id for i in items] == ["it-1", "it-2"]
    assert [i.display_number for i in items] == [1, 2]
