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
import logging
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

    async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
        captured["action"] = action
        captured["params"] = params
        captured["timeout_s"] = timeout_s
        captured["on_late_response"] = on_late_response
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


@pytest.mark.parametrize(
    "send_exc", [asyncio.TimeoutError(), RuntimeError("boom")],
    ids=["timeout", "send-failure"],
)
def test_send_fault_companion_log_is_below_error(send_exc, caplog):
    """One send_request fault must produce ONE ERROR record, not two.

    WheelHouseApp.send_request already logs every timeout / send failure at
    ERROR before re-raising, and the error-notification handler turns EVERY
    ERROR record into its own Windows notification popup. When this awaiter
    logged its companion line at ERROR too, one fault popped two
    notifications (observed live 2026-08-08 on a start_overlay_walk timeout;
    wh-duplicate-error-popup). The companion line keeps its context in the
    log -- at WARNING.
    """
    c = _make_controller(send_exc=send_exc)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(c.forward_click_element(_query(), "trace-lvl"))
    companions = [
        r for r in caplog.records
        if "no reply within" in r.getMessage()
        or "send_request failed" in r.getMessage()
    ]
    assert companions, "expected the awaiter's companion log line"
    assert all(r.levelno < logging.ERROR for r in companions)


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
# Late-answer correction (wh-overlay-slow-uia-stale-badges.6, Option A).
#
# The awaiter passes on_late_response to send_request. When the true answer
# arrives after the timeout (inside app.py's grace window), a NON-OK answer
# forwards the corrected notice -- the toast singleton replaces the visible
# "timed out" text -- and an OK answer forwards nothing (INFO log only).
# ---------------------------------------------------------------------------


def _notices(controller):
    """Return every ClickNoticeEvent forwarded to the GUI, in order."""
    q = controller.state_manager.state_to_gui_queue.put_nowait
    events = []
    for call in q.call_args_list:
        msg = call[0][0]
        assert msg["action"] == "show_click_notice"
        events.append(ClickNoticeEvent.from_dict(
            {k: v for k, v in msg.items() if k != "action"}
        ))
    return events


def _late_response(outcome, *, reason=None, matched_name=None,
                   trace_id="trace-late"):
    return ClickElementResponse(
        status="ok" if outcome == "ok" else "error",
        outcome=outcome,
        reason=reason,
        matched_names=(),
        snapshot_id=None,
        snapshot_summary=None,
        matched_name=matched_name,
        trace_id=trace_id,
    ).to_dict()


def test_awaiter_passes_late_response_callback():
    c = _make_controller(send_exc=asyncio.TimeoutError())
    asyncio.run(c.forward_click_element(_query(), "trace-late"))
    assert callable(c._captured["on_late_response"])


def test_late_non_ok_response_forwards_corrected_notice():
    # The incident shape: transport timeout notice first, then the true
    # refusal arrives late. The late callback must forward the refusal's
    # reason through the same response-to-notice mapping the on-time path
    # uses, so the corrected notice replaces the "timed out" toast text.
    c = _make_controller(send_exc=asyncio.TimeoutError())
    asyncio.run(c.forward_click_element(_query(), "trace-late"))
    cb = c._captured["on_late_response"]
    cb(_late_response(
        "execution_failed", reason="bounds_invalid", matched_name="OK",
    ))
    events = _notices(c)
    assert len(events) == 2
    assert events[0].reason == "timeout"
    assert events[1].outcome == "execution_failed"
    assert events[1].reason == "bounds_invalid"
    assert events[1].matched_name == "OK"
    assert events[1].spoken_name == "cancel"
    assert events[1].trace_id == "trace-late"


def test_late_ok_response_forwards_no_notice():
    # Decided residual: a late OK gets an INFO log only -- no success-notice
    # mechanism exists, and the user saw the click happen.
    c = _make_controller(send_exc=asyncio.TimeoutError())
    asyncio.run(c.forward_click_element(_query(), "trace-late"))
    cb = c._captured["on_late_response"]
    cb(_late_response("ok", matched_name="OK"))
    events = _notices(c)
    assert len(events) == 1
    assert events[0].reason == "timeout"


def test_late_malformed_response_forwards_no_notice():
    # A malformed late payload must not replace the (accurate) timeout notice
    # with a less specific one; it logs and leaves the toast alone.
    c = _make_controller(send_exc=asyncio.TimeoutError())
    asyncio.run(c.forward_click_element(_query(), "trace-late"))
    cb = c._captured["on_late_response"]
    cb({"garbage": True})
    events = _notices(c)
    assert len(events) == 1
    assert events[0].reason == "timeout"


def test_late_response_with_summary_populates_snapshot_cache():
    # wh-overlay-slow-uia-stale-badges.20.3(b): a late parsed response
    # carrying snapshot_id + snapshot_summary must populate the summary cache
    # exactly like the on-time put, so the badge resolution can read it.
    c = _make_controller(send_exc=asyncio.TimeoutError())
    asyncio.run(c.forward_click_element(_query(), "trace-late"))
    cb = c._captured["on_late_response"]
    late = ClickElementResponse(
        status="error",
        outcome="execution_failed",
        reason="bounds_invalid",
        matched_names=(),
        snapshot_id="walk-9",
        snapshot_summary=_summary("walk-9"),
        matched_name="OK",
        trace_id="trace-late",
    ).to_dict()
    cb(late)
    result = c.click_snapshot_summary_cache.resolve("walk-9")
    assert result.summary is not None
    assert result.summary.snapshot_id == "walk-9"


def test_late_ambiguous_does_not_auto_open_overlay():
    # DECIDED (wh-overlay-slow-uia-stale-badges.20.3): a late ambiguous reply
    # does NOT auto-open the numbered overlay -- an overlay appearing seconds
    # after the command, unprompted, is worse than the corrected notice
    # alone. Only the corrected ambiguous notice is forwarded.
    c = _make_controller(send_exc=asyncio.TimeoutError())
    asyncio.run(c.forward_click_element(_query(), "trace-late"))
    cb = c._captured["on_late_response"]
    cb(_late_response("ambiguous", trace_id="trace-late"))
    c._perform_auto_open_ambiguous.assert_not_called()
    events = _notices(c)
    assert len(events) == 2
    assert events[1].outcome == "ambiguous"


def test_late_correction_suppressed_when_newer_notice_forwarded(caplog):
    # wh-overlay-slow-uia-stale-badges.20.4: a newer notice (another click's
    # feedback) was forwarded between the timeout and the late answer. The
    # correction must be suppressed with an INFO log naming both trace ids,
    # not replace the newer, still-accurate notice.
    c = _make_controller(send_exc=asyncio.TimeoutError())
    asyncio.run(c.forward_click_element(_query(), "trace-late"))
    cb = c._captured["on_late_response"]
    # A newer click's notice lands after the timeout notice.
    c._forward_click_notice(
        outcome="execution_failed", reason="disabled", matched_name="Cancel",
        matched_names=(), spoken_name="cancel", snapshot_id=None,
        trace_id="trace-newer",
    )
    with caplog.at_level(logging.INFO):
        cb(_late_response(
            "execution_failed", reason="bounds_invalid", trace_id="trace-late",
        ))
    events = _notices(c)
    assert len(events) == 2  # the timeout + the newer notice; no correction
    assert events[1].trace_id == "trace-newer"
    suppressed = [
        r for r in caplog.records
        if "trace-newer" in r.getMessage() and "trace-late" in r.getMessage()
    ]
    assert suppressed, "expected the suppression log naming both trace ids"


def test_late_refusal_forwarded_when_timeout_notice_put_failed():
    # wh-overlay-slow-uia-stale-badges.20.10 (A-fail-nothing-newer): A's
    # timeout notice never left Logic (the GUI queue put raised Full) and no
    # newer notice was forwarded. A's late refusal is then the FIRST
    # feedback for the newest click, not a stale overwrite -- it must be
    # forwarded. Comparing only the last SUCCESSFUL trace cannot see this.
    from queue import Full

    c = _make_controller(send_exc=asyncio.TimeoutError())
    q = c.state_manager.state_to_gui_queue.put_nowait
    q.side_effect = [Full("queue full"), None]
    asyncio.run(c.forward_click_element(_query(), "trace-late"))
    assert q.call_count == 1  # the timeout notice attempt failed

    cb = c._captured["on_late_response"]
    cb(_late_response(
        "execution_failed", reason="bounds_invalid", trace_id="trace-late",
    ))
    assert q.call_count == 2  # the late refusal was retried as feedback
    forwarded = q.call_args_list[1][0][0]
    assert forwarded["action"] == "show_click_notice"
    assert forwarded["reason"] == "bounds_invalid"
    # The successful late put records the trace like any forwarded notice.
    assert c._last_click_notice_trace_id == "trace-late"


def test_late_refusal_suppressed_when_newer_notice_succeeded_after_failed_attempt(
    caplog,
):
    # wh-overlay-slow-uia-stale-badges.20.10 (A-fail-then-B2-success): A's
    # timeout notice put failed, but a NEWER click's notice B2 succeeded
    # afterwards. B2's notice is on the toast; A's late correction must
    # still be suppressed, with the INFO log naming the ids.
    from queue import Full

    c = _make_controller(send_exc=asyncio.TimeoutError())
    q = c.state_manager.state_to_gui_queue.put_nowait
    q.side_effect = [Full("queue full"), None, None]
    asyncio.run(c.forward_click_element(_query(), "trace-late"))
    assert q.call_count == 1  # A's timeout notice attempt failed
    # A newer click's notice succeeds.
    c._forward_click_notice(
        outcome="execution_failed", reason="disabled", matched_name="Cancel",
        matched_names=(), spoken_name="cancel", snapshot_id=None,
        trace_id="trace-newer",
    )
    assert q.call_count == 2

    cb = c._captured["on_late_response"]
    with caplog.at_level(logging.INFO):
        cb(_late_response(
            "execution_failed", reason="bounds_invalid", trace_id="trace-late",
        ))
    assert q.call_count == 2  # no third put: the correction was suppressed
    suppressed = [
        r for r in caplog.records
        if "trace-newer" in r.getMessage() and "trace-late" in r.getMessage()
    ]
    assert suppressed, "expected the suppression log naming the trace ids"


def test_older_late_correction_does_not_erase_a_newer_failed_attempt():
    # wh-overlay-slow-uia-stale-badges.20.11: click A times out and its
    # timeout notice reaches the GUI; click B times out later and its own
    # timeout notice put FAILS (queue full). A's late refusal is then
    # correctly admitted -- the toast still shows A's timeout wording -- but
    # forwarding it must NOT move the attempt marker back to A, or B's late
    # refusal matches neither marker and B never gets any true feedback.
    from queue import Full

    c = _make_controller(send_exc=asyncio.TimeoutError())
    q = c.state_manager.state_to_gui_queue.put_nowait
    q.side_effect = [None, Full("queue full"), None, None]

    asyncio.run(c.forward_click_element(_query(), "trace-a"))
    cb_a = c._captured["on_late_response"]
    asyncio.run(c.forward_click_element(_query(), "trace-b"))
    cb_b = c._captured["on_late_response"]
    assert q.call_count == 2  # A's notice shown, B's notice dropped

    cb_a(_late_response(
        "execution_failed", reason="bounds_invalid", trace_id="trace-a",
    ))
    assert q.call_count == 3  # A's correction replaces A's timeout wording

    cb_b(_late_response(
        "execution_failed", reason="disabled", trace_id="trace-b",
    ))
    assert q.call_count == 4, (
        "B's late refusal must still be forwarded: the older correction for "
        "A must not erase B's failed notice attempt"
    )
    forwarded = q.call_args_list[3][0][0]
    assert forwarded["trace_id"] == "trace-b"
    assert forwarded["reason"] == "disabled"


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
