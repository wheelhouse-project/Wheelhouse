"""Logic-side awaiter tests for the voice 'click <target>' flow (wh-tab7j).

These cover LogicController.forward_click_element -- the Logic half of the
command-to-response click flow:

  * Disabled-by-config short-circuit: ClickConfig.enabled=false ->
    execution_failed:disabled_by_config notice, shown once per session, no IPC.
  * Timeout path: the Input handler never replies -> the awaiter emits
    execution_failed:timeout after the configured window.
  * Timeout-config regression: the click round trip uses the configured
    [click].response_timeout_ms (default 3000ms -> 3.0s), NOT
    WheelHouseApp.response_timeout_s (5s). Proven by asserting the timeout_s
    send_request received.
  * Malformed-response path: a payload that fails ClickElementResponse.from_dict
    -> the awaiter logs the truncated payload and emits
    execution_failed:malformed_response. No unhandled exception escapes.
  * Happy path: a status=ok ClickElementResponse flows back -> no notice, and
    the snapshot summary is retained in the snapshot-summary cache.
  * Non-ok outcomes (not_found / ambiguous / walk-time execution_failed)
    forward a ClickNoticeEvent carrying the trace_id.

The Input-side walk -> decide -> click composition lives in
tests/test_ui/test_click_element_handler.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from shared.click_element import ClickElementResponse
from shared.click_notice import ClickNoticeEvent
from ui.click_config import ClickConfig
from ui.element_types import (
    ElementQuery,
    WalkSnapshotSummary,
    WalkSnapshotSummaryItem,
)
# Import the cache via the full services.wheelhouse.* package path -- the same
# path LogicController.__init__ uses to build self.click_snapshot_summary_cache
# and that _handle_snapshot_item_clicked uses for the resolver. Importing it
# bare here would build the cache from a DIFFERENT module object than the
# handler's resolver, so the resolver's CacheStatus.HIT identity check would
# fail and the snapshot tests would not faithfully exercise the production
# module identity (wh-9f3t.70.1).
from services.wheelhouse.click_snapshot_summary_cache import (
    ClickSnapshotSummaryCache,
)


def _query(name="cancel", role="Button"):
    return ElementQuery(name, role, None, None, name)


def _summary(snapshot_id="walk-1"):
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id,
        items=[
            WalkSnapshotSummaryItem(
                item_id="m1", display_number=1, name="Cancel",
                role="Button", bounds=(10, 20, 80, 30), monitor_id=0,
            )
        ],
        created_at_monotonic=123.0,
    )


def _make_controller(*, enabled=True, response_timeout_ms=3000,
                     send_result=None, send_exc=None):
    """Build a MagicMock(spec=LogicController) with the click methods bound.

    ``send_result`` is the dict the fake app.send_request resolves to;
    ``send_exc`` is an exception it raises instead (e.g. asyncio.TimeoutError).
    """
    from main import LogicController

    c = MagicMock(spec=LogicController)
    c.forward_click_element = (
        LogicController.forward_click_element.__get__(c)
    )
    c._forward_click_notice = (
        LogicController._forward_click_notice.__get__(c)
    )

    cfg = ClickConfig.from_raw(
        {"enabled": enabled, "response_timeout_ms": response_timeout_ms}
    )
    c.click_config = cfg
    c.click_snapshot_summary_cache = ClickSnapshotSummaryCache(
        ttl_seconds=float(cfg.snapshot_ttl_seconds),
    )
    c._click_disabled_notice_shown = False

    c.state_manager = MagicMock()
    c.state_manager.state_to_gui_queue = MagicMock()

    captured = {}

    async def _send_request(action, params=None, timeout_s=None):
        captured["action"] = action
        captured["params"] = params
        captured["timeout_s"] = timeout_s
        if send_exc is not None:
            raise send_exc
        return send_result

    c.app = MagicMock()
    c.app.send_request = _send_request
    c._captured = captured
    return c


def _notice(controller):
    """Return the ClickNoticeEvent forwarded to the GUI, or None."""
    q = controller.state_manager.state_to_gui_queue.put_nowait
    if q.call_count == 0:
        return None
    msg = q.call_args[0][0]
    assert msg["action"] == "show_click_notice"
    return ClickNoticeEvent.from_dict(
        {k: v for k, v in msg.items() if k != "action"}
    )


# ---------------------------------------------------------------------------
# Disabled-by-config short-circuit.
# ---------------------------------------------------------------------------


def test_disabled_by_config_short_circuits_and_notifies():
    c = _make_controller(enabled=False)
    asyncio.run(c.forward_click_element(_query(), "trace-cfg"))

    # No IPC was sent (the gate is pre-IPC).
    assert "action" not in c._captured
    notice = _notice(c)
    assert notice is not None
    assert notice.outcome == "execution_failed"
    assert notice.reason == "disabled_by_config"
    assert notice.spoken_name == "cancel"
    assert notice.trace_id == "trace-cfg"


def test_disabled_by_config_notice_is_one_shot_per_session():
    c = _make_controller(enabled=False)
    asyncio.run(c.forward_click_element(_query(), "t1"))
    asyncio.run(c.forward_click_element(_query(), "t2"))
    # Only the first attempt shows the notice.
    assert c.state_manager.state_to_gui_queue.put_nowait.call_count == 1


# ---------------------------------------------------------------------------
# Timeout path + the response_timeout_ms regression.
# ---------------------------------------------------------------------------


def test_timeout_emits_execution_failed_timeout():
    c = _make_controller(send_exc=asyncio.TimeoutError())
    asyncio.run(c.forward_click_element(_query(), "trace-to"))
    notice = _notice(c)
    assert notice is not None
    assert notice.outcome == "execution_failed"
    assert notice.reason == "timeout"
    assert notice.trace_id == "trace-to"


def test_uses_click_response_timeout_ms_not_app_default():
    # Regression: the click round trip must use [click].response_timeout_ms
    # (3000ms -> 3.0s), NOT WheelHouseApp.response_timeout_s (5.0s default).
    c = _make_controller(response_timeout_ms=3000, send_exc=asyncio.TimeoutError())
    asyncio.run(c.forward_click_element(_query(), "trace-cfg"))
    assert c._captured["action"] == "click_element"
    assert c._captured["timeout_s"] == pytest.approx(3.0)
    assert c._captured["timeout_s"] != 5.0


def test_custom_response_timeout_ms_is_honoured():
    c = _make_controller(response_timeout_ms=1500, send_exc=asyncio.TimeoutError())
    asyncio.run(c.forward_click_element(_query(), "t"))
    assert c._captured["timeout_s"] == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Malformed-response path.
# ---------------------------------------------------------------------------


def test_malformed_response_emits_malformed_response_notice(caplog):
    # A payload that fails ClickElementResponse.from_dict (missing fields).
    c = _make_controller(send_result={"status": "ok"})  # missing required fields
    asyncio.run(c.forward_click_element(_query(), "trace-mal"))
    notice = _notice(c)
    assert notice is not None
    assert notice.outcome == "execution_failed"
    assert notice.reason == "malformed_response"
    assert notice.trace_id == "trace-mal"


def test_malformed_response_does_not_raise():
    # A wholly wrong type must not escape the asyncio task.
    c = _make_controller(send_result=12345)
    # Should complete without raising.
    asyncio.run(c.forward_click_element(_query(), "t"))
    notice = _notice(c)
    assert notice is not None and notice.reason == "malformed_response"


def test_malformed_response_log_excludes_on_screen_values(caplog):
    # wh-9f3t.55.2: a malformed payload can carry on-screen control / window
    # text in matched_names or snapshot_summary item names. The structural
    # summary log must NOT emit those values, while staying diagnosable.
    import logging

    secret = "SUPER_SECRET_WINDOW_TITLE_xyz"
    bad = {
        "status": "ok",  # missing required fields -> from_dict fails
        "matched_names": [secret, "another_secret_control_name"],
        "snapshot_summary": {"items": [{"name": secret}]},
    }
    c = _make_controller(send_result=bad)
    with caplog.at_level(logging.ERROR):
        asyncio.run(c.forward_click_element(_query(), "trace-priv"))

    notice = _notice(c)
    assert notice is not None and notice.reason == "malformed_response"
    # The on-screen text must not appear anywhere in the captured logs.
    assert secret not in caplog.text
    # The log stays diagnosable: the field names and the trace id appear.
    assert "matched_names" in caplog.text
    assert "trace-priv" in caplog.text


def test_send_request_runtime_error_degrades_to_notice():
    c = _make_controller(send_exc=RuntimeError("UI process error"))
    asyncio.run(c.forward_click_element(_query(), "t"))
    notice = _notice(c)
    assert notice is not None
    assert notice.outcome == "execution_failed"
    # A non-timeout send failure carries its own reason, not the misleading
    # timeout tag (wh-9f3t.56.2).
    assert notice.reason == "send_request_failed"


# ---------------------------------------------------------------------------
# Happy path: ok response -> no notice, cache populated.
# ---------------------------------------------------------------------------


def test_ok_response_shows_no_notice_and_populates_cache():
    ok = ClickElementResponse(
        status="ok", outcome="ok", reason=None,
        matched_names=("Cancel",), snapshot_id="walk-1",
        snapshot_summary=_summary("walk-1"), matched_name="Cancel",
        trace_id="trace-ok",
    )
    c = _make_controller(send_result=ok.to_dict())
    asyncio.run(c.forward_click_element(_query(), "trace-ok"))

    # No notice for a successful click.
    assert _notice(c) is None
    # The snapshot summary was retained for the Phase 1.5 overlay round trip.
    result = c.click_snapshot_summary_cache.resolve("walk-1")
    assert result.summary is not None
    assert result.summary.snapshot_id == "walk-1"


# ---------------------------------------------------------------------------
# Non-ok outcomes forward a ClickNoticeEvent.
# ---------------------------------------------------------------------------


def test_not_found_forwards_notice_with_trace_id():
    nf = ClickElementResponse(
        status="ok", outcome="not_found", reason=None,
        matched_names=(), snapshot_id="walk-2",
        snapshot_summary=_summary("walk-2"), matched_name=None,
        trace_id="trace-nf",
    )
    c = _make_controller(send_result=nf.to_dict())
    asyncio.run(c.forward_click_element(_query(), "trace-nf"))
    notice = _notice(c)
    assert notice is not None
    assert notice.outcome == "not_found"
    assert notice.spoken_name == "cancel"
    assert notice.trace_id == "trace-nf"


def test_ambiguous_forwards_matched_names():
    amb = ClickElementResponse(
        status="ok", outcome="ambiguous", reason=None,
        matched_names=("Cancel", "Cancel all"), snapshot_id="walk-3",
        snapshot_summary=_summary("walk-3"), matched_name=None,
        trace_id="trace-amb",
    )
    c = _make_controller(send_result=amb.to_dict())
    asyncio.run(c.forward_click_element(_query(), "trace-amb"))
    notice = _notice(c)
    assert notice is not None
    assert notice.outcome == "ambiguous"
    assert notice.matched_names == ("Cancel", "Cancel all")


def test_execution_failed_forwards_reason_and_matched_name():
    ef = ClickElementResponse(
        status="error", outcome="execution_failed", reason="disabled",
        matched_names=("Submit",), snapshot_id="walk-4",
        snapshot_summary=None, matched_name="Submit",
        trace_id="trace-ef",
    )
    c = _make_controller(send_result=ef.to_dict())
    asyncio.run(c.forward_click_element(_query(), "trace-ef"))
    notice = _notice(c)
    assert notice is not None
    assert notice.outcome == "execution_failed"
    assert notice.reason == "disabled"
    assert notice.matched_name == "Submit"


# ---------------------------------------------------------------------------
# Action-level: ActionFunctions.click_element generates a trace_id and
# delegates to LogicController.forward_click_element (or falls through to
# dictation on unparseable input).
# ---------------------------------------------------------------------------


def _action_funcs():
    from speech.actions import ActionFunctions

    handler = MagicMock()
    lc = MagicMock()
    lc.forward_click_element = MagicMock()
    handler.logic_controller = lc
    return ActionFunctions(handler), lc


def test_action_unparseable_returns_none_and_does_not_delegate():
    from utils.trace_context import set_trace

    set_trace("")  # clear any contextvar bleed from a prior test
    funcs, lc = _action_funcs()
    # "the" collapses to an empty name -> ClickCommandParser.parse returns None.
    result = asyncio.run(funcs.click_element("the"))
    assert result is None
    lc.forward_click_element.assert_not_called()


def test_action_parseable_generates_trace_and_delegates():
    from utils.trace_context import set_trace

    set_trace("")
    funcs, lc = _action_funcs()

    captured = {}

    async def _fwd(query, trace_id):
        captured["query"] = query
        captured["trace_id"] = trace_id

    lc.forward_click_element = _fwd

    asyncio.run(funcs.click_element("the cancel button"))
    assert captured["query"].name == "cancel"
    assert captured["query"].role == "Button"
    # A trace_id was generated (click-scoped when none was pre-set).
    assert captured["trace_id"]
    assert captured["trace_id"].startswith("click-")


def test_action_reuses_existing_pipeline_trace_id():
    from utils.trace_context import set_trace

    set_trace("utt-existing-trace")
    funcs, lc = _action_funcs()

    captured = {}

    async def _fwd(query, trace_id):
        captured["trace_id"] = trace_id

    lc.forward_click_element = _fwd
    asyncio.run(funcs.click_element("save"))
    assert captured["trace_id"] == "utt-existing-trace"
    set_trace("")  # reset for any later test


# ---------------------------------------------------------------------------
# Phase 1.5 numbered-overlay click: _handle_snapshot_item_clicked
# (wh-click-snapshot-expired-emit). The handler resolves a snapshot_id +
# display_number against the retained snapshot cache and maps the outcome:
#   SNAPSHOT_EXPIRED -> execution_failed:snapshot_expired notice.
#   NOT_FOUND        -> collapsed into the same snapshot_expired notice.
#   FOUND            -> dispatch the real click_snapshot_item, no notice on the
#                       resolve itself (wh-n29v.95; the click-notice on a non-ok
#                       click outcome is owned by _send_snapshot_item_click).
#   malformed payload -> logged and dropped, no raise.
# ---------------------------------------------------------------------------


def _make_snapshot_controller():
    """MagicMock(spec=LogicController) with the snapshot-click methods bound."""
    from main import LogicController

    c = MagicMock(spec=LogicController)
    c._handle_snapshot_item_clicked = (
        LogicController._handle_snapshot_item_clicked.__get__(c)
    )
    c._forward_click_notice = (
        LogicController._forward_click_notice.__get__(c)
    )
    c.click_snapshot_summary_cache = ClickSnapshotSummaryCache(ttl_seconds=30.0)
    c.state_manager = MagicMock()
    c.state_manager.state_to_gui_queue = MagicMock()
    return c


def _clicked(snapshot_id="walk-1", display_number=1):
    return {
        "action": "snapshot_item_clicked",
        "snapshot_id": snapshot_id,
        "display_number": display_number,
    }


def test_snapshot_item_clicked_expired_forwards_snapshot_expired_notice():
    # Nothing in the cache -> resolver returns SNAPSHOT_EXPIRED.
    c = _make_snapshot_controller()
    asyncio.run(c._handle_snapshot_item_clicked(_clicked("gone", 1)))
    notice = _notice(c)
    assert notice is not None
    assert notice.outcome == "execution_failed"
    assert notice.reason == "snapshot_expired"
    assert notice.snapshot_id == "gone"


def test_snapshot_item_clicked_not_found_collapses_into_snapshot_expired():
    # A live snapshot whose items do NOT carry display_number 9 -> NOT_FOUND,
    # collapsed into the same snapshot_expired notice.
    c = _make_snapshot_controller()
    c.click_snapshot_summary_cache.put("walk-1", _summary("walk-1"))
    asyncio.run(c._handle_snapshot_item_clicked(_clicked("walk-1", 9)))
    notice = _notice(c)
    assert notice is not None
    assert notice.outcome == "execution_failed"
    assert notice.reason == "snapshot_expired"
    assert notice.snapshot_id == "walk-1"


def test_snapshot_item_clicked_found_dispatches_click_and_emits_no_notice():
    # A live snapshot with display_number 1 -> FOUND. wh-n29v.95: the handler
    # now dispatches the real click_snapshot_item with the resolved item_id, and
    # emits NO notice on the resolve itself (any non-ok click outcome is surfaced
    # later by _send_snapshot_item_click, not here).
    c = _make_snapshot_controller()
    c.click_snapshot_summary_cache.put("walk-1", _summary("walk-1"))
    asyncio.run(c._handle_snapshot_item_clicked(_clicked("walk-1", 1)))
    # The FOUND branch dispatched the click with the resolved (snapshot, item).
    c._dispatch_snapshot_item_click.assert_called_once()
    _, kwargs = c._dispatch_snapshot_item_click.call_args
    assert kwargs.get("snapshot_id") == "walk-1"
    assert kwargs.get("item_id") == "m1"
    # No notice on the resolve.
    assert _notice(c) is None


def test_snapshot_item_clicked_malformed_payload_is_dropped_without_raising():
    c = _make_snapshot_controller()
    # Missing display_number -> SnapshotItemClickedSchemaError -> logged + dropped.
    bad = {"action": "snapshot_item_clicked", "snapshot_id": "walk-1"}
    asyncio.run(c._handle_snapshot_item_clicked(bad))
    assert _notice(c) is None


def test_snapshot_item_clicked_unexpected_error_degrades_without_shutdown():
    # wh-9f3t.69.3: an unexpected (non-ValueError) exception after the payload
    # parses must be swallowed -- the handler runs as a
    # create_task_with_error_handling background task whose done-callback would
    # otherwise restart the whole Logic process. An advisory click notice must
    # degrade (log + drop), not escalate. The four tests above call the bound
    # method directly, bypassing the wrapper, so this asserts the in-handler
    # try/except directly: a raising resolve_display_number neither propagates
    # nor forwards a notice.
    from unittest.mock import patch

    c = _make_snapshot_controller()
    with patch(
        # Patch the package-path module the handler imports the resolver from
        # (wh-9f3t.70.1); patching the bare module would no longer intercept it.
        "services.wheelhouse.click_snapshot_summary_cache.resolve_display_number",
        side_effect=RuntimeError("boom"),
    ):
        # Must not raise out of the handler (would reach request_shutdown).
        asyncio.run(c._handle_snapshot_item_clicked(_clicked("walk-1", 1)))
    # Degrade-don't-die: the click is dropped, no notice forwarded.
    assert _notice(c) is None


def test_snapshot_item_clicked_found_uses_production_package_path_cache():
    # wh-9f3t.70.1 regression: the handler resolves the display number against
    # self.click_snapshot_summary_cache (built in __init__ from the
    # services.wheelhouse.* package path) using resolve_display_number, which
    # compares the cache's CacheStatus.HIT by identity. If the handler imported
    # the resolver from the BARE module while the cache came from the package
    # module (or vice versa), Python's two module objects would carry two
    # distinct CacheStatus enums, the HIT identity check would fail, and a live
    # cache HIT (which should take the FOUND no-notice path) would misresolve as
    # execution_failed:snapshot_expired. Build the cache here via the same
    # package path production uses and assert FOUND emits no notice; a reverted
    # bare import in the handler makes this test forward a snapshot_expired
    # notice and fail.
    from services.wheelhouse.click_snapshot_summary_cache import (
        ClickSnapshotSummaryCache as ProdCache,
    )

    c = _make_snapshot_controller()
    c.click_snapshot_summary_cache = ProdCache(ttl_seconds=30.0)
    c.click_snapshot_summary_cache.put("walk-1", _summary("walk-1"))
    asyncio.run(c._handle_snapshot_item_clicked(_clicked("walk-1", 1)))
    assert _notice(c) is None


# ---------------------------------------------------------------------------
# wh-n29v.122: pipeline observability. A successfully forwarded notice must
# write ONE INFO line naming outcome, reason, and trace_id -- before this, a
# sent AND rendered notice wrote zero Logic-process log lines (both existing
# logs are failure-only), so a live session could not tell "never sent" from
# "sent and missed while the toast auto-dismissed".
# ---------------------------------------------------------------------------


def test_forward_click_notice_logs_info_on_successful_put(caplog):
    import logging

    c = _make_controller()
    with caplog.at_level(logging.INFO):
        c._forward_click_notice(
            outcome="execution_failed",
            reason="bounds_stale",
            matched_name="Submit",
            matched_names=(),
            spoken_name="",
            snapshot_id="snap-1",
            trace_id="tr-log",
        )
    assert c.state_manager.state_to_gui_queue.put_nowait.call_count == 1
    records = [
        r for r in caplog.records
        if r.levelno == logging.INFO
        and "click notice forwarded" in r.getMessage()
    ]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "execution_failed" in msg
    assert "bounds_stale" in msg
    assert "tr-log" in msg


def test_forward_click_notice_no_info_when_put_fails(caplog):
    """The INFO line must sit AFTER the successful queue put: a failed put
    keeps the existing WARNING and emits no 'forwarded' line, so the log
    never claims a notice reached the GUI when it did not."""
    import logging

    c = _make_controller()
    c.state_manager.state_to_gui_queue.put_nowait.side_effect = RuntimeError(
        "queue full"
    )
    with caplog.at_level(logging.INFO):
        c._forward_click_notice(
            outcome="execution_failed",
            reason="bounds_stale",
            matched_name="Submit",
            matched_names=(),
            spoken_name="",
            snapshot_id="snap-1",
            trace_id="tr-log2",
        )
    assert not [
        r for r in caplog.records
        if "click notice forwarded" in r.getMessage()
    ]
    assert any(
        r.levelno == logging.WARNING and "tr-log2" in r.getMessage()
        for r in caplog.records
    )
