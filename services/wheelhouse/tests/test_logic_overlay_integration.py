"""Logic-side effect-performing overlay integration tests (wh-n29v.95).

This slice replaces the three Logic-process STUB SEAMS that previously only
LOGGED -- ``_dispatch_snapshot_item_click``, ``_hold_click_n``, and
``_perform_overlay_effects`` -- with the real effect-performing integration so a
numbered-overlay item click executes end to end:

  * Part 1: a real ``click_snapshot_item`` send to Input for BOTH the voice
    ``click N`` path and the GUI-consumer FOUND branch, consuming the
    ``ClickElementResponse`` and forwarding the click-notice on any non-ok
    outcome (no notice on ok).
  * Part 2: the async build-dispatch performer (start_overlay_walk vs
    show_numbered_overlay by BuildReason) + per-state asyncio timeout timers, so
    a standalone 'show numbers' reaches PAINTED. Effect dispatch is serialized
    under an asyncio.Lock so a not-yet-completed clear from one ack is not
    reordered against a paint/clear from a later ack (wh-n29v.70.2).
  * Part 3: the 200ms hold-or-drop timer + state re-read for a 'click N' that
    arrives during a transition; never a silent drop.
  * Part 4: the build awaiters populate the summary cache, and the
    actively-painted snapshot is kept alive past the 30s TTL.
  * Part 5: ``_overlay_tracked_identity`` is assigned at the pin point and
    cleared on entry to closed.

The controllers are built via ``object.__new__`` to skip the heavy ``__init__``
(the wh-n29v test precedent in test_logic_overlay_state_changed_handler.py),
injecting only the attributes each path touches. ``app.send_request`` and the
GUI state queue are fakes; effects and IPC are asserted as DATA.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, cast
from unittest.mock import MagicMock

import pytest

from services.wheelhouse.click_overlay_state import (
    BuildReason,
    ClickOverlayStateMachine,
    Effect,
    EffectKind,
    OverlayEvent,
    OverlayEventKind,
    OverlayState,
    PaintAckState,
)
from services.wheelhouse.click_snapshot_summary_cache import (
    ClickSnapshotSummaryCache,
)
from services.wheelhouse.main import (
    LogicController,
    _OVERLAY_READ_SENTENCE_WAIT_MAX_S,
    _OVERLAY_RESTORE_ACK_DEADLINE_MS,
    _OVERLAY_RESTORE_SCHEDULE_BUDGET_MAX_MS,
    _OVERLAY_REWALK_REFUSAL_REASONS,
)
from services.wheelhouse.shared.click_element import ClickElementResponse
from services.wheelhouse.shared.show_numbered_overlay import (
    ShowNumberedOverlayResponse,
)
from services.wheelhouse.shared.start_overlay_walk import (
    StartOverlayWalkResponse,
)
from services.wheelhouse.ui.element_types import (
    WalkSnapshotSummary,
    WalkSnapshotSummaryItem,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summary(snapshot_id: str, *display_numbers: int) -> WalkSnapshotSummary:
    items = [
        WalkSnapshotSummaryItem(
            item_id=f"{snapshot_id}-item-{n}",
            display_number=n,
            name=f"control {n}",
            role="Button",
            bounds=(0, 0, 10, 10),
            monitor_id=0,
        )
        for n in display_numbers
    ]
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id, items=items, created_at_monotonic=1.0,
    )


class _FakeQueue:
    """A minimal state_to_gui_queue capturing put_nowait payloads."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_nowait(self, item: dict) -> None:
        self.items.append(item)


def _controller(
    *,
    enabled: bool = True,
    overlay_effective: bool = True,
    cache: Optional[ClickSnapshotSummaryCache] = None,
    machine: Optional[ClickOverlayStateMachine] = None,
):
    """Build a bare LogicController with only the integration attributes.

    Skips the heavy ``__init__`` via ``object.__new__`` and injects the few
    attributes the effect performer / awaiters / hold timer touch.
    """

    controller = object.__new__(LogicController)
    controller.click_config = MagicMock()
    controller.click_config.enabled = enabled
    controller.click_config.overlay_enabled_effective = overlay_effective
    controller.click_config.overlay_invalid_key = None
    controller.click_config.response_timeout_ms = 3000
    controller.click_config.screen_read_timeout_ms = 10000
    # NB: use ``is None`` checks, NOT ``x or default`` -- an empty
    # ClickSnapshotSummaryCache has __len__ == 0 and is therefore FALSY, so
    # ``cache or ClickSnapshotSummaryCache()`` would silently DISCARD a freshly
    # constructed (empty) injected cache and substitute a different instance.
    controller.click_overlay_state = (
        machine if machine is not None else ClickOverlayStateMachine()
    )
    controller.click_snapshot_summary_cache = (
        cache if cache is not None else ClickSnapshotSummaryCache()
    )
    controller._overlay_keepalive_interval_s = 15.0
    # Effect serialization + timer registry (the integration creates these
    # lazily, but inject them so a bypassed __init__ controller has them).
    controller._overlay_effect_lock = asyncio.Lock()
    controller._overlay_timer = None
    controller._overlay_timer_pair = None
    controller._overlay_armed_timer_state = None
    # wh-overlay-timer-cancel-race: the monotonic per-arm id whose
    # value the timer callback captures, so a superseded callback can
    # tell that it is no longer the live arm.
    controller._overlay_timer_arm_id = 0
    controller._overlay_hold_timer = None
    controller._overlay_keepalive_timer = None
    controller._overlay_focus_debouncer = MagicMock()
    controller._overlay_settle_handle = None
    controller._overlay_focus_hooks = None
    controller._overlay_destroy_hook_active = False
    controller._overlay_tracked_identity = None
    # wh-overlay-rewalk-after-filter.1.3: the window an in-flight build
    # is for, until the pin makes the tracked identity truthful.
    controller._overlay_pending_build_identity = None
    controller._overlay_snapshot_window_identity = {}
    # wh-overlay-slow-uia-stale-badges.2.2.1: the window a settle reply
    # reported it was read from, held between the commit fence and the pin.
    controller._overlay_settle_read_identity = None
    controller._overlay_pending_postclick_refresh = None
    # wh-overlay-slow-uia-stale-badges.15: the deferred post-click clear.
    controller._overlay_pending_postclick_clear = None
    controller._overlay_auto_open_filter = None
    # wh-overlay-slow-uia-stale-badges.9: pending-clear watchdog state.
    controller._overlay_pending_clear = None
    controller._overlay_pending_clear_timer = None
    controller._overlay_clear_fault_reported_pair = None
    # wh-overlay-slow-uia-stale-badges.21.3: restore-paint delivery audit.
    controller._overlay_pending_restore_paint = None
    controller._overlay_pending_restore_paint_timer = None
    # wh-overlay-slow-uia-stale-badges.21.8: and its delivery entitlement.
    controller._overlay_restore_paint_entitlement = None
    # Fake GUI queue.
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = _FakeQueue()
    return controller


async def _settle(controller, *, turns: int = 12) -> None:
    """Pump the loop until overlay background tasks settle (test harness).

    The build-response feed is now deferred via ``loop.call_soon`` (wh-n29v.96.4),
    and that deferred transition schedules a FRESH OverlayEffects task (PIN/PAINT)
    that is not in any earlier ``background_tasks`` snapshot. A single
    ``await asyncio.gather(*background_tasks)`` therefore misses the follow-on
    work. This yields repeatedly, gathering newly-spawned tasks each turn, until
    no pending overlay task remains (bounded so a bug cannot hang the test).
    """

    for _ in range(turns):
        await asyncio.sleep(0)
        pending = [t for t in controller.background_tasks if not t.done()]
        if not pending:
            # Yield once more in case a call_soon callback is still queued.
            await asyncio.sleep(0)
            if all(t.done() for t in controller.background_tasks):
                break
        else:
            await asyncio.gather(*pending, return_exceptions=True)


def _gui_items(controller) -> list[dict]:
    """Return the captured GUI-queue payloads as a typed list.

    ``controller.state_manager`` is a MagicMock whose ``state_to_gui_queue`` is
    a ``_FakeQueue``; pyright sees the declared StateManager type, so cast to
    the fake to read ``.items`` cleanly.
    """

    queue = cast(_FakeQueue, controller.state_manager.state_to_gui_queue)
    return queue.items


def _wire_app(controller, responder):
    """Attach a fake app whose send_request defers to ``responder``.

    ``responder(action, params)`` returns the wire dict (a *.to_dict()).
    Captures every call on controller._sent (list of (action, params)).
    """

    sent: list[tuple[str, dict]] = []

    async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
        sent.append((action, dict(params or {})))
        return responder(action, params or {})

    controller.app = MagicMock()
    controller.app.send_request = _send_request
    controller._sent = sent  # type: ignore[attr-defined]
    return sent


def _set_trace_noop(monkeypatch=None):
    pass


# ---------------------------------------------------------------------------
# PART 1: real click_snapshot_item send (voice path + GUI-consumer FOUND).
# ---------------------------------------------------------------------------


def _click_response(outcome: str, *, reason=None, matched_name=None,
                    trace_id="trace") -> dict:
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


def test_dispatch_snapshot_item_click_sends_and_no_notice_on_ok():
    controller = _controller()
    _wire_app(controller, lambda a, p: _click_response("ok"))
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

    asyncio.run(
        controller._send_snapshot_item_click(
            snapshot_id="snap", item_id="snap-item-2", trace_id="tr-1",
        )
    )

    assert controller._sent[0][0] == "click_snapshot_item"  # type: ignore[attr-defined]
    params = controller._sent[0][1]  # type: ignore[attr-defined]
    assert params["snapshot_id"] == "snap"
    assert params["item_id"] == "snap-item-2"
    assert params["trace_id"] == "tr-1"
    cast(MagicMock, controller._forward_click_notice).assert_not_called()


@pytest.mark.parametrize(
    "outcome,reason",
    [
        ("not_found", None),
        ("ambiguous", None),
        ("execution_failed", "invoke_com_error"),
    ],
)
def test_send_snapshot_item_click_forwards_notice_on_non_ok(outcome, reason):
    controller = _controller()
    _wire_app(
        controller, lambda a, p: _click_response(outcome, reason=reason)
    )
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

    asyncio.run(
        controller._send_snapshot_item_click(
            snapshot_id="snap", item_id="snap-item-1", trace_id="tr-2",
        )
    )

    notice = cast(MagicMock, controller._forward_click_notice)
    notice.assert_called_once()
    _, kwargs = notice.call_args
    assert kwargs.get("outcome") == outcome


def test_send_snapshot_item_click_malformed_response_notice():
    controller = _controller()
    _wire_app(controller, lambda a, p: {"garbage": True})
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

    asyncio.run(
        controller._send_snapshot_item_click(
            snapshot_id="snap", item_id="i", trace_id="tr-3",
        )
    )
    notice = cast(MagicMock, controller._forward_click_notice)
    notice.assert_called_once()
    _, kwargs = notice.call_args
    assert kwargs.get("outcome") == "execution_failed"
    assert kwargs.get("reason") == "malformed_response"


def test_send_snapshot_item_click_timeout_notice():
    controller = _controller()

    async def _raise(action, params=None, timeout_s=None,
                     on_late_response=None):
        raise asyncio.TimeoutError()

    controller.app = MagicMock()
    controller.app.send_request = _raise
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

    asyncio.run(
        controller._send_snapshot_item_click(
            snapshot_id="snap", item_id="i", trace_id="tr-4",
        )
    )
    notice = cast(MagicMock, controller._forward_click_notice)
    notice.assert_called_once()
    _, kwargs = notice.call_args
    assert kwargs.get("reason") == "timeout"


def _timeout_with_late_capture(controller):
    """Wire a send_request that times out and captures on_late_response."""
    captured: dict = {}

    async def _raise(action, params=None, timeout_s=None,
                     on_late_response=None):
        captured["on_late_response"] = on_late_response
        raise asyncio.TimeoutError()

    controller.app = MagicMock()
    controller.app.send_request = _raise
    return captured


def test_send_snapshot_item_click_late_non_ok_forwards_true_reason():
    """wh-overlay-slow-uia-stale-badges.6 Option A, badge path: the true
    refusal arriving after the timeout forwards the corrected notice (the
    toast singleton replaces the 'timed out' text)."""
    controller = _controller()
    captured = _timeout_with_late_capture(controller)
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
    # The mocked forwarder does not record the last-forwarded trace_id, so
    # model the record the real _forward_click_notice would have left after
    # the timeout notice (wh-overlay-slow-uia-stale-badges.20.4 gate).
    controller._last_click_notice_trace_id = "tr-late"

    asyncio.run(
        controller._send_snapshot_item_click(
            snapshot_id="snap", item_id="i", trace_id="tr-late",
        )
    )
    notice = cast(MagicMock, controller._forward_click_notice)
    assert notice.call_count == 1  # the timeout notice
    cb = captured["on_late_response"]
    assert callable(cb)
    cb(_click_response(
        "execution_failed", reason="bounds_invalid", matched_name="OK",
        trace_id="tr-late",
    ))
    assert notice.call_count == 2
    _, kwargs = notice.call_args
    assert kwargs.get("outcome") == "execution_failed"
    assert kwargs.get("reason") == "bounds_invalid"
    assert kwargs.get("matched_name") == "OK"
    # The late response carries no snapshot_id; the dispatch-time id is the
    # fallback, exactly as on the on-time non-ok path.
    assert kwargs.get("snapshot_id") == "snap"
    assert kwargs.get("trace_id") == "tr-late"


def test_send_snapshot_item_click_late_ok_forwards_nothing():
    """Decided residual: a late OK badge click gets an INFO log only."""
    controller = _controller()
    captured = _timeout_with_late_capture(controller)
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
    # Model the record the real _forward_click_notice would have left after
    # the timeout notice. The ok path returns BEFORE the .20.4 staleness
    # gate, so this changes nothing post-fix -- it exists so the mutation
    # gate's M4 (delete the ok-branch return) is caught by THIS test's
    # call-count assertion rather than masked by the gate suppressing the
    # fallthrough notice.
    controller._last_click_notice_trace_id = "tr-late"

    asyncio.run(
        controller._send_snapshot_item_click(
            snapshot_id="snap", item_id="i", trace_id="tr-late",
        )
    )
    cb = captured["on_late_response"]
    cb(_click_response("ok", trace_id="tr-late"))
    notice = cast(MagicMock, controller._forward_click_notice)
    assert notice.call_count == 1  # only the timeout notice


def test_send_snapshot_item_click_late_rewalk_reason_feeds_refresh():
    """wh-overlay-slow-uia-stale-badges.20.3(a): a late refusal whose reason
    is in _OVERLAY_REWALK_REFUSAL_REASONS must drive the same post-click
    re-walk the on-time tail drives -- with the dispatch-time pair -- so the
    overlay stops painting the stale badges the refusal is about."""
    controller = _controller()
    captured = _timeout_with_late_capture(controller)
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
    controller._feed_click_complete_refresh = MagicMock()  # type: ignore[method-assign]
    # Model the record the real _forward_click_notice would have left after
    # the timeout notice (wh-overlay-slow-uia-stale-badges.20.4 gate).
    controller._last_click_notice_trace_id = "tr-late"

    asyncio.run(
        controller._send_snapshot_item_click(
            snapshot_id="snap", item_id="i", trace_id="tr-late",
            overlay_dispatch_pair=(7, 3),
        )
    )
    cb = captured["on_late_response"]
    cb(_click_response(
        "execution_failed", reason="bounds_invalid", trace_id="tr-late",
    ))
    feed = cast(MagicMock, controller._feed_click_complete_refresh)
    feed.assert_called_once_with(
        overlay_dispatch_pair=(7, 3),
        trace_id="tr-late",
        trigger="click refused (bounds_invalid)",
        # wh-overlay-slow-uia-stale-badges.15: the pre-click snapshot id rides
        # along so a same-session generation bump can defer the clear instead
        # of dropping the reply. On the late path it is fallback_snapshot_id.
        preclick_snapshot_id="snap",
        # wh-overlay-slow-uia-stale-badges.23.3: and this click's dispatch
        # ordinal, the key that orders two outstanding clicks.
        overlay_dispatch_ordinal=1,
    )


def test_send_snapshot_item_click_late_non_rewalk_reason_no_refresh():
    """A late refusal with a reason OUTSIDE _OVERLAY_REWALK_REFUSAL_REASONS
    forwards the corrected notice but must NOT drive a re-walk (matches the
    on-time tail's reason gate)."""
    controller = _controller()
    captured = _timeout_with_late_capture(controller)
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
    controller._feed_click_complete_refresh = MagicMock()  # type: ignore[method-assign]
    controller._last_click_notice_trace_id = "tr-late"

    asyncio.run(
        controller._send_snapshot_item_click(
            snapshot_id="snap", item_id="i", trace_id="tr-late",
            overlay_dispatch_pair=(7, 3),
        )
    )
    cb = captured["on_late_response"]
    cb(_click_response(
        "execution_failed", reason="invoke_com_error", trace_id="tr-late",
    ))
    notice = cast(MagicMock, controller._forward_click_notice)
    assert notice.call_count == 2  # the timeout notice + the correction
    cast(
        MagicMock, controller._feed_click_complete_refresh,
    ).assert_not_called()


def test_send_snapshot_item_click_late_ok_with_pair_runs_success_bookkeeping():
    """wh-overlay-slow-uia-stale-badges.20.8: a late OK badge answer must
    mirror the on-time success bookkeeping -- cancel a held 'click N' armed
    at the dispatch pair and feed the CLICK_COMPLETE refresh, with the same
    arguments the on-time ok branch passes -- while the notice behaviour
    stays INFO-only (no success notice, no correction)."""
    controller = _controller()
    captured = _timeout_with_late_capture(controller)
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
    controller._cancel_held_click_after_success = MagicMock()  # type: ignore[method-assign]
    controller._feed_click_complete_refresh = MagicMock()  # type: ignore[method-assign]
    controller._last_click_notice_trace_id = "tr-late"

    asyncio.run(
        controller._send_snapshot_item_click(
            snapshot_id="snap", item_id="i", trace_id="tr-late",
            overlay_dispatch_pair=(7, 3),
        )
    )
    cb = captured["on_late_response"]
    cb(_click_response("ok", trace_id="tr-late"))

    # The SAME argument shape as the on-time ok branch (trigger omitted on
    # the feed -- the success path uses the default).
    cast(
        MagicMock, controller._cancel_held_click_after_success,
    ).assert_called_once_with(
        overlay_dispatch_pair=(7, 3), trace_id="tr-late",
    )
    cast(
        MagicMock, controller._feed_click_complete_refresh,
    ).assert_called_once_with(
        overlay_dispatch_pair=(7, 3), trace_id="tr-late",
        # wh-overlay-slow-uia-stale-badges.15, as above.
        preclick_snapshot_id="snap",
        # wh-overlay-slow-uia-stale-badges.23.3: and this click's dispatch
        # ordinal, the key that orders two outstanding clicks.
        overlay_dispatch_ordinal=1,
    )
    notice = cast(MagicMock, controller._forward_click_notice)
    assert notice.call_count == 1  # only the timeout notice


def test_send_snapshot_item_click_late_ok_superseded_pair_feed_noops():
    """wh-overlay-slow-uia-stale-badges.20.8 (superseded case): the late OK
    bookkeeping goes through the REAL feed, whose generation/session gate
    makes a superseded dispatch pair a safe no-op -- the newer painted
    overlay is untouched."""

    async def _run():
        machine = ClickOverlayStateMachine()
        # The click was dispatched against this (old) painted overlay.
        old_sid, old_gen = _drive_to_painted(machine, "snap-old")
        # A focus-change supersede bumps the generation; a NEW overlay is
        # painted at a different pair.
        machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
        new_sid, new_gen = machine.overlay_session_id, machine.paint_generation
        machine.apply(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=new_sid,
                paint_generation=new_gen, snapshot_id="snap-new",
            )
        )
        machine.apply(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=new_sid,
                paint_generation=new_gen, paint_state=PaintAckState.PAINTED,
            )
        )
        assert machine.state is OverlayState.PAINTED
        assert (new_sid, new_gen) != (old_sid, old_gen)

        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        captured = _timeout_with_late_capture(controller)
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        controller._last_click_notice_trace_id = "tr-sup"

        await controller._send_snapshot_item_click(
            snapshot_id="snap-old", item_id="snap-old-item-2",
            trace_id="tr-sup", overlay_dispatch_pair=(old_sid, old_gen),
        )
        captured["on_late_response"](_click_response("ok", trace_id="tr-sup"))
        await asyncio.sleep(0)
        return machine, (new_sid, new_gen)

    machine, (new_sid, new_gen) = asyncio.run(_run())
    # The superseded late ok did NOT refresh or clear the newer overlay.
    assert machine.state is OverlayState.PAINTED
    assert machine.overlay_session_id == new_sid
    assert machine.paint_generation == new_gen


def test_send_snapshot_item_click_late_suppressed_notice_still_feeds_refresh():
    """wh-overlay-slow-uia-stale-badges.20.9: a newer notice suppresses A's
    late corrective NOTICE, but the refusal is still evidence the painted
    badges are stale -- the badge re-walk feed must run independent of the
    staleness gate. No third notice is forwarded."""
    controller = _controller()
    captured = _timeout_with_late_capture(controller)
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
    controller._feed_click_complete_refresh = MagicMock()  # type: ignore[method-assign]
    # A NEWER click's notice (B) was forwarded after A's timeout notice:
    # model both records the real forwarder would have left.
    controller._last_click_notice_trace_id = "tr-newer"
    controller._last_click_notice_attempt_trace_id = "tr-newer"

    asyncio.run(
        controller._send_snapshot_item_click(
            snapshot_id="snap", item_id="i", trace_id="tr-late",
            overlay_dispatch_pair=(7, 3),
        )
    )
    cb = captured["on_late_response"]
    cb(_click_response(
        "execution_failed", reason="bounds_invalid", trace_id="tr-late",
    ))

    feed = cast(MagicMock, controller._feed_click_complete_refresh)
    feed.assert_called_once_with(
        overlay_dispatch_pair=(7, 3),
        trace_id="tr-late",
        trigger="click refused (bounds_invalid)",
        # wh-overlay-slow-uia-stale-badges.15: the pre-click snapshot id rides
        # along so a same-session generation bump can defer the clear instead
        # of dropping the reply. On the late path it is fallback_snapshot_id.
        preclick_snapshot_id="snap",
        # wh-overlay-slow-uia-stale-badges.23.3: and this click's dispatch
        # ordinal, the key that orders two outstanding clicks.
        overlay_dispatch_ordinal=1,
    )
    notice = cast(MagicMock, controller._forward_click_notice)
    assert notice.call_count == 1  # the timeout notice; correction suppressed


@pytest.mark.parametrize(
    "send_exc", [asyncio.TimeoutError(), RuntimeError("boom")],
    ids=["timeout", "send-failure"],
)
def test_send_snapshot_item_click_fault_log_is_below_error(send_exc, caplog):
    """One send_request fault must produce ONE ERROR record, not two.

    WheelHouseApp.send_request already logs every timeout / send failure at
    ERROR before re-raising, and the error-notification handler turns EVERY
    ERROR record into its own Windows notification popup
    (wh-duplicate-error-popup). The awaiter's companion line stays in the
    log at WARNING.
    """
    controller = _controller()

    async def _raise(action, params=None, timeout_s=None,
                     on_late_response=None):
        raise send_exc

    controller.app = MagicMock()
    controller.app.send_request = _raise
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            controller._send_snapshot_item_click(
                snapshot_id="snap", item_id="i", trace_id="tr-lvl",
            )
        )
    companions = [
        r for r in caplog.records
        if "no reply within" in r.getMessage()
        or "send_request failed" in r.getMessage()
    ]
    assert companions, "expected the awaiter's companion log line"
    assert all(r.levelno < logging.ERROR for r in companions)


def test_dispatch_snapshot_item_click_schedules_send():
    """The synchronous voice-path seam schedules the real async send."""

    controller = _controller()

    async def _run():
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        captured = {}

        async def _fake_send(*, snapshot_id, item_id, trace_id,
                             overlay_dispatch_pair=None,
                             overlay_resolved_pair=None, gesture=None):
            captured["snapshot_id"] = snapshot_id
            captured["item_id"] = item_id
            captured["trace_id"] = trace_id

        controller._send_snapshot_item_click = _fake_send  # type: ignore[method-assign]
        controller._dispatch_snapshot_item_click(
            snapshot_id="snap", item_id="snap-item-2", trace_id="tr-5",
        )
        await asyncio.sleep(0)
        await asyncio.gather(*controller.background_tasks)
        return captured

    captured = asyncio.run(_run())
    assert captured["snapshot_id"] == "snap"
    assert captured["item_id"] == "snap-item-2"


def test_handle_snapshot_item_clicked_found_dispatches_click():
    """The GUI-consumer FOUND branch now dispatches the real click."""

    cache = ClickSnapshotSummaryCache()
    cache.put("snap", _summary("snap", 1, 2, 3))
    controller = _controller(cache=cache)
    captured = {}

    def _dispatch(*, snapshot_id, item_id, trace_id):
        captured["snapshot_id"] = snapshot_id
        captured["item_id"] = item_id

    controller._dispatch_snapshot_item_click = _dispatch  # type: ignore[method-assign]
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

    from services.wheelhouse.shared.snapshot_item_clicked import (
        SnapshotItemClickedEvent,
    )

    command = SnapshotItemClickedEvent(
        snapshot_id="snap", display_number=2
    ).to_dict()
    asyncio.run(controller._handle_snapshot_item_clicked(command))

    assert captured.get("snapshot_id") == "snap"
    assert captured.get("item_id") == "snap-item-2"
    # FOUND -> dispatch, no notice.
    cast(MagicMock, controller._forward_click_notice).assert_not_called()


def test_handle_snapshot_item_clicked_expired_still_notices():
    """A non-FOUND resolve still surfaces the snapshot_expired notice."""

    controller = _controller(cache=ClickSnapshotSummaryCache())
    controller._dispatch_snapshot_item_click = MagicMock()  # type: ignore[method-assign]
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

    from services.wheelhouse.shared.snapshot_item_clicked import (
        SnapshotItemClickedEvent,
    )

    command = SnapshotItemClickedEvent(
        snapshot_id="gone", display_number=1
    ).to_dict()
    asyncio.run(controller._handle_snapshot_item_clicked(command))

    cast(MagicMock, controller._dispatch_snapshot_item_click).assert_not_called()
    notice = cast(MagicMock, controller._forward_click_notice)
    notice.assert_called_once()
    _, kwargs = notice.call_args
    assert kwargs.get("reason") == "snapshot_expired"


# ---------------------------------------------------------------------------
# PART 2: the async build-dispatch performer + timers (RELEASE GATE).
# ---------------------------------------------------------------------------


def _walk_response(snapshot_id, sid, gen, *display_numbers) -> dict:
    return StartOverlayWalkResponse(
        status="ok",
        outcome="ok",
        reason=None,
        snapshot_id=snapshot_id,
        snapshot_summary=_summary(snapshot_id, *display_numbers),
        trace_id="tr",
        overlay_session_id=sid,
        paint_generation=gen,
    ).to_dict()


def _show_response(snapshot_id, sid, gen, *display_numbers) -> dict:
    return ShowNumberedOverlayResponse(
        status="ok",
        outcome="ok",
        reason=None,
        snapshot_id=snapshot_id,
        snapshot_summary=_summary(snapshot_id, *display_numbers),
        trace_id="tr",
        overlay_session_id=sid,
        paint_generation=gen,
    ).to_dict()


def test_dispatch_build_show_numbers_sends_start_overlay_walk():
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        def _respond(action, params):
            if action == "start_overlay_walk":
                return _walk_response("snap-w", sid, gen, 1, 2)
            return _pin_response(action, params)

        _wire_app(controller, _respond)
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SHOW_NUMBERS,
        )
        await controller._dispatch_overlay_effects((effect,), trace_id="tr")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    sent_actions = [a for a, _ in controller._sent]  # type: ignore[attr-defined]
    assert "start_overlay_walk" in sent_actions
    # The build response fed BUILD_RESPONSE back -> paint_in_flight, and the
    # summary cache was populated.
    assert machine.state is OverlayState.PAINT_IN_FLIGHT
    assert (
        controller.click_snapshot_summary_cache.resolve("snap-w").summary
        is not None
    )


def _record_build_timeouts(controller, sid, gen):
    """Wire an app whose send_request records (action, timeout_s) per call."""
    recorded: list[tuple[str, float | None]] = []

    async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
        recorded.append((action, timeout_s))
        if action == "start_overlay_walk":
            return _walk_response("snap-w", sid, gen, 1, 2)
        if action == "show_numbered_overlay":
            return _show_response("snap-a", sid, gen, 1)
        return _pin_response(action, dict(params or {}))

    controller.app = MagicMock()
    controller.app.send_request = _send_request
    return recorded


def test_screen_read_build_awaits_its_own_key_and_auto_open_awaits_the_reply_limit():
    """wh-overlay-slow-uia-stale-badges.3, acceptance criterion 3.

    A ``start_overlay_walk`` build IS the screen read, so Logic must await it
    for ``[click] screen_read_timeout_ms``; a read that answers at 7 s is then
    used instead of discarded at the 3 s reply limit. ``AUTO_OPEN`` sends
    ``show_numbered_overlay``, a lookup in the existing snapshot store with no
    walk behind it, so it keeps ``response_timeout_ms``.
    """
    read_ms = 8000
    reply_ms = 3000

    async def _run_walk():
        machine = ClickOverlayStateMachine()
        machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        sid, gen = machine.overlay_session_id, machine.paint_generation
        controller = _controller(machine=machine)
        controller.click_config.response_timeout_ms = reply_ms
        controller.click_config.screen_read_timeout_ms = read_ms
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        recorded = _record_build_timeouts(controller, sid, gen)
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SHOW_NUMBERS,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr")
        await _settle(controller)
        return recorded

    async def _run_auto_open():
        from services.wheelhouse.shared.click_notice import ClickNoticeEvent

        machine = ClickOverlayStateMachine()
        machine.apply(
            OverlayEvent(
                OverlayEventKind.AUTO_OPEN,
                notice=ClickNoticeEvent(
                    outcome="ambiguous", reason=None, matched_name=None,
                    matched_names=("a", "b"), spoken_name="x",
                    app_friendly_name="App", snapshot_id="snap-a",
                    trace_id="tr",
                ),
                snapshot_id="snap-a",
            )
        )
        sid, gen = machine.overlay_session_id, machine.paint_generation
        controller = _controller(machine=machine)
        controller.click_config.response_timeout_ms = reply_ms
        controller.click_config.screen_read_timeout_ms = read_ms
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        recorded = _record_build_timeouts(controller, sid, gen)
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.AUTO_OPEN,
            snapshot_id="snap-a",
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr")
        await _settle(controller)
        return recorded

    walk_calls = asyncio.run(_run_walk())
    walk_timeouts = [t for a, t in walk_calls if a == "start_overlay_walk"]
    assert walk_timeouts == [read_ms / 1000.0], (
        "the screen read was awaited for "
        f"{walk_timeouts!r} s, not the configured {read_ms / 1000.0} s; a "
        "correct read that answers after the reply limit is discarded"
    )

    auto_calls = asyncio.run(_run_auto_open())
    show_timeouts = [t for a, t in auto_calls if a == "show_numbered_overlay"]
    assert show_timeouts == [reply_ms / 1000.0]


class _WalkDeadlineRig:
    """A ``controller.loop`` stand-in that records the machine's timer arms.

    Every attribute but ``call_later`` forwards to the real running loop. A
    ``call_later`` is recorded with the state its ARM_TIMER effect named --
    ``_overlay_arm_timer`` stamps ``_overlay_armed_timer_state`` immediately
    before it calls this -- and a WALK_IN_FLIGHT deadline whose delay is
    shorter than the simulated read is FIRED at once, because a deadline of
    2.5 s expires while a read that answers at 7.0 s is still running.

    Firing at the ARM is what covers today's effect order: the CLOSED
    SHOW_NUMBERS transition emits ARM_TIMER *after* DISPATCH_BUILD, so
    nothing is armed while ``send_request`` runs and the arm lands between
    the reply's return and the deferred BUILD_RESPONSE the build schedules
    with ``loop.call_soon`` (wh-n29v.96.4). A future change that moves
    ARM_TIMER ahead of the build -- the reordering
    ``make_click_overlay_state_machine`` derives the deadline against -- is
    covered by the same rule. Either order puts the TIMEOUT before the point
    at which the reply is APPLIED, which is what this test reads.

    The handles are Mocks and nothing is scheduled on the real loop, so a
    deadline this rig does not fire cannot fire late and cannot hang.
    """

    def __init__(self, loop, controller, delivery_s: float) -> None:
        self._loop = loop
        self._controller = controller
        self._delivery_s = delivery_s
        self.arms: list[tuple[Any, float]] = []
        self.walk_deadlines: list[float] = []
        self.fired: list[float] = []

    def __getattr__(self, name):
        return getattr(self._loop, name)

    def _is_walk_deadline(self, state) -> bool:
        machine = self._controller.click_overlay_state
        return (
            state is OverlayState.WALK_IN_FLIGHT
            and machine.state is OverlayState.WALK_IN_FLIGHT
        )

    def call_later(self, delay, callback, *args):
        state = getattr(
            self._controller, "_overlay_armed_timer_state", None
        )
        self.arms.append((state, delay))
        if self._is_walk_deadline(state):
            self.walk_deadlines.append(delay)
            if delay <= self._delivery_s:
                self.fired.append(delay)
                callback(*args)
        return MagicMock()


def test_a_read_that_answers_after_the_reply_limit_but_inside_the_read_limit_is_applied():
    """wh-overlay-slow-uia-stale-badges.3.1.3: the 7 s read is USED.

    The sibling above records the ``timeout_s`` the request carries while its
    fake answers at once, and journey 7 copies the request frame and never
    answers at all. Neither delivers a reply in the window the bead is about,
    so neither shows that a valid ``start_overlay_walk`` answer arriving at
    7.0 s -- after the 3000 ms reply limit, after the 2500 ms walk deadline
    the machine used before the derivation, and inside the 8000 ms read limit
    -- reaches BUILD_RESPONSE, the summary cache and the paint.

    This drives the shipped 'show numbers' entry point with the machine built
    the way LogicController builds it (``make_click_overlay_state_machine``,
    so ``walk_deadline_ms`` is the derived 8250 and not a literal) and puts
    the reply through BOTH guards that can throw it away:

      * the awaited window -- the fake raises ``asyncio.TimeoutError`` when
        the ``timeout_s`` it was handed is under the 7.0 s the read takes; and
      * the machine's walk deadline -- ``_WalkDeadlineRig`` fires a
        WALK_IN_FLIGHT deadline shorter than that same 7.0 s, so a deadline
        still derived from the reply limit turns the reply into a stale
        completion.

    Time is simulated at both guards, so the test crosses a 7-second read
    without sleeping.
    """

    from services.wheelhouse.main import make_click_overlay_state_machine
    from services.wheelhouse.ui.click_config import ClickConfig

    delivery_s = 7.0
    config = ClickConfig.from_raw(
        {
            "enabled": True,
            "screen_read_timeout_ms": 8000,
            "response_timeout_ms": 3000,
        }
    )
    assert config.invalid_key is None
    applied: list = []
    sent: list[tuple[str, "float | None"]] = []

    async def _run():
        machine = make_click_overlay_state_machine(config)
        controller = _controller(machine=machine)
        controller.click_config.response_timeout_ms = (
            config.response_timeout_ms
        )
        controller.click_config.screen_read_timeout_ms = (
            config.screen_read_timeout_ms
        )
        rig = _WalkDeadlineRig(
            asyncio.get_running_loop(), controller, delivery_s
        )
        controller.loop = rig
        controller.background_tasks = []
        controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
            return_value=None
        )
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        real_apply = controller._apply_overlay_event

        def _record_apply(event, *, source):
            applied.append(event)
            return real_apply(event, source=source)

        controller._apply_overlay_event = _record_apply  # type: ignore[method-assign]

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            params = dict(params or {})
            sent.append((action, timeout_s))
            if action != "start_overlay_walk":
                return _pin_response(action, params)
            # The read takes ``delivery_s``; an awaiter shorter than that
            # never sees the answer.
            if timeout_s is None or timeout_s < delivery_s:
                raise asyncio.TimeoutError()
            return _walk_response(
                "snap-slow",
                params["overlay_session_id"],
                params["paint_generation"],
                1,
                2,
            )

        controller.app = MagicMock()
        controller.app.send_request = _send_request

        await controller.handle_overlay_command("show", "tr-slow")
        await _settle(controller)
        return controller, machine, rig

    controller, machine, rig = asyncio.run(_run())

    walk_timeouts = [t for a, t in sent if a == "start_overlay_walk"]
    builds = [
        e for e in applied if e.kind is OverlayEventKind.BUILD_RESPONSE
    ]
    timeouts = [e for e in applied if e.kind is OverlayEventKind.TIMEOUT]
    gui_actions = [m.get("action") for m in _gui_items(controller)]
    detail = (
        f"awaited={walk_timeouts!r}s walk_deadlines={rig.walk_deadlines!r}s "
        f"fired={rig.fired!r} build_ok={[e.build_ok for e in builds]!r} "
        f"timeouts={len(timeouts)} state={machine.state.value} "
        f"gui={gui_actions!r}"
    )

    assert machine.state is OverlayState.PAINT_IN_FLIGHT, (
        "a correct read that answered at 7.0 s was discarded: the machine "
        f"ended in {machine.state.value}, not paint_in_flight -- {detail}"
    )
    assert [e.build_ok for e in builds] == [True], (
        "a correct read that answered at 7.0 s was discarded: BUILD_RESPONSE "
        f"did not carry a single build_ok=True -- {detail}"
    )
    assert timeouts == [], (
        "a correct read that answered at 7.0 s was discarded: a TIMEOUT "
        "event was applied while the read was still inside its own limit "
        f"-- {detail}"
    )
    assert (
        controller.click_snapshot_summary_cache.resolve("snap-slow").summary
        is not None
    ), (
        "a correct read that answered at 7.0 s was discarded: its snapshot "
        f"never reached the summary cache -- {detail}"
    )
    assert "paint_overlay" in gui_actions, (
        "a correct read that answered at 7.0 s was discarded: no "
        f"paint_overlay was sent to the GUI -- {detail}"
    )
    assert rig.walk_deadlines == [8.25], (
        "a correct read that answered at 7.0 s was discarded: the machine "
        f"armed {rig.walk_deadlines!r} s for walk_in_flight, not the 8.25 s "
        "derived from screen_read_timeout_ms, so the walk state expires "
        f"before the read it is waiting on -- {detail}"
    )


def test_dispatch_build_settle_sends_the_settle_flag_and_the_compare_id():
    """Acceptance criterion 2: the Input side is asked for a SETTLED re-read.

    ``settle=True`` is what makes the handler run the shared settle detector
    instead of one plain walk, and ``compare_snapshot_id`` is what it compares
    the settled read against. Both are read from the EFFECT: the machine pin
    can move between the effect being built and this async performer running,
    the same reason AUTO_OPEN's reuse id rides the effect (wh-n29v.96.1). The
    live machine pin is set to a DIFFERENT id here so a performer that read
    the pin instead of the effect fails.
    """
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation
    machine.pinned_snapshot_id = "snap-moved-on"

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        def _respond(action, params):
            if action == "start_overlay_walk":
                return _walk_response("snap-held", sid, gen, 1, 2)
            return _pin_response(action, params)

        _wire_app(controller, _respond)
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SETTLE,
            snapshot_id="snap-held",
        )
        await controller._dispatch_overlay_effects((effect,), trace_id="tr")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    walks = [
        params for action, params in controller._sent  # type: ignore[attr-defined]
        if action == "start_overlay_walk"
    ]
    assert len(walks) == 1
    assert walks[0]["settle"] is True
    assert walks[0]["compare_snapshot_id"] == "snap-held"


def test_dispatch_build_show_numbers_asks_for_no_settle():
    """Criterion 7's Logic-side half: only SETTLE changes the request.

    A refresh or a fresh show-numbers walk must reach Input exactly as it did
    before this bead, so the shipped path is untouched when the flag is off.
    """
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        def _respond(action, params):
            if action == "start_overlay_walk":
                return _walk_response("snap-w", sid, gen, 1, 2)
            return _pin_response(action, params)

        _wire_app(controller, _respond)
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SHOW_NUMBERS,
        )
        await controller._dispatch_overlay_effects((effect,), trace_id="tr")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    walks = [
        params for action, params in controller._sent  # type: ignore[attr-defined]
        if action == "start_overlay_walk"
    ]
    assert len(walks) == 1
    assert walks[0].get("settle", False) is False
    assert not walks[0].get("compare_snapshot_id", "")


def test_dispatch_build_auto_open_sends_show_numbered_overlay():
    # wh-n29v.96.1 (FINDING 1): AUTO_OPEN fires from CLOSED where the machine
    # pin is None, so the reuse snapshot id must travel on the DISPATCH_BUILD
    # effect (effect.snapshot_id) and be read from THERE -- NOT from the live
    # machine pin. The machine pin is left None here on purpose.
    machine = ClickOverlayStateMachine()
    from services.wheelhouse.shared.click_notice import ClickNoticeEvent

    notice = ClickNoticeEvent(
        outcome="ambiguous", reason=None, matched_name=None,
        matched_names=("a", "b"), spoken_name="x", app_friendly_name="App",
        snapshot_id="snap-a", trace_id="tr",
    )
    machine.apply(
        OverlayEvent(
            OverlayEventKind.AUTO_OPEN, notice=notice, snapshot_id="snap-a",
        )
    )
    assert machine.pinned_snapshot_id is None  # not pinned until build returns
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        def _respond(action, params):
            if action == "show_numbered_overlay":
                return _show_response("snap-a", sid, gen, 1)
            return _pin_response(action, params)

        _wire_app(controller, _respond)
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.AUTO_OPEN,
            snapshot_id="snap-a",  # the reuse id the machine stamped
        )
        await controller._dispatch_overlay_effects((effect,), trace_id="tr")
        await asyncio.sleep(0)
        await asyncio.gather(*controller.background_tasks)
        return controller

    controller = asyncio.run(_run())
    sent = controller._sent  # type: ignore[attr-defined]
    show = [(a, p) for a, p in sent if a == "show_numbered_overlay"]
    assert len(show) == 1
    # The reuse snapshot id was threaded from the effect, not the (None) pin.
    assert show[0][1]["snapshot_id"] == "snap-a"


def test_dispatch_paint_puts_paint_overlay_on_gui_queue():
    cache = ClickSnapshotSummaryCache()
    cache.put("snap-p", _summary("snap-p", 1, 2))
    # wh-overlay-slow-uia-stale-badges.19: the paint dispatcher now fences on
    # the machine's own bookkeeping, so the fixture must present the machine
    # state a real DISPATCH_PAINT is emitted from -- the pair the effect
    # carries, with the effect's snapshot as the pinned (intended-visible) one.
    machine = ClickOverlayStateMachine()
    machine.state = OverlayState.PAINT_IN_FLIGHT
    machine.overlay_session_id = 5
    machine.paint_generation = 2
    machine.pinned_snapshot_id = "snap-p"
    controller = _controller(cache=cache, machine=machine)
    _wire_app(controller, lambda a, p: {})

    effect = Effect(
        kind=EffectKind.DISPATCH_PAINT,
        overlay_session_id=5,
        paint_generation=2,
        snapshot_id="snap-p",
    )
    asyncio.run(
        controller._dispatch_overlay_effects((effect,), trace_id="tr")
    )
    items = _gui_items(controller)
    assert any(m.get("action") == "paint_overlay" for m in items)
    paint = [m for m in items if m.get("action") == "paint_overlay"][0]
    # WalkSnapshotSummary is FLATTENED into the top-level dict.
    assert paint["snapshot_id"] == "snap-p"
    assert paint["overlay_session_id"] == 5
    assert paint["paint_generation"] == 2
    assert "items" in paint


def test_dispatch_clear_puts_clear_overlay_on_gui_queue():
    controller = _controller()
    _wire_app(controller, lambda a, p: {})
    effect = Effect(
        kind=EffectKind.DISPATCH_CLEAR,
        overlay_session_id=3,
        paint_generation=1,
    )
    asyncio.run(
        controller._dispatch_overlay_effects((effect,), trace_id="tr")
    )
    clear = [
        m for m in _gui_items(controller)
        if m.get("action") == "clear_overlay"
    ]
    assert len(clear) == 1
    assert clear[0]["overlay_session_id"] == 3


def test_pin_effect_sends_pin_and_assigns_tracked_identity():
    controller = _controller()
    _wire_app(controller, lambda a, p: _pin_response(a, p))
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value="IDENTITY"
    )
    effect = Effect(
        kind=EffectKind.PIN_SNAPSHOT,
        overlay_session_id=7,
        paint_generation=0,
        snapshot_id="snap-x",
    )
    asyncio.run(
        controller._dispatch_overlay_effects((effect,), trace_id="tr")
    )
    assert controller._sent[0][0] == "pin_snapshot"  # type: ignore[attr-defined]
    params = controller._sent[0][1]  # type: ignore[attr-defined]
    assert params["snapshot_id"] == "snap-x"
    assert params["overlay_session_id"] == 7
    # Part 5: the pin point assigns the tracked identity.
    assert controller._overlay_tracked_identity == "IDENTITY"


def test_unpin_effect_sends_unpin():
    controller = _controller()
    _wire_app(controller, lambda a, p: _pin_response(a, p))
    effect = Effect(
        kind=EffectKind.UNPIN_SNAPSHOT,
        overlay_session_id=7,
        snapshot_id="snap-x",
    )
    asyncio.run(
        controller._dispatch_overlay_effects((effect,), trace_id="tr")
    )
    assert controller._sent[0][0] == "unpin_snapshot"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Trigger B (wh-overlay-snapshot-keepalive): per-snapshot window identity +
# the refresh-window-mismatch helper that feeds route_click_n. A focus-change
# refresh leaves the still-visible snapshot pinned to a window that is no longer
# foreground; routing must HOLD a 'click N' rather than dispatch a click Input
# would reject on a foreground-identity mismatch (snapshot_expired).
# ---------------------------------------------------------------------------


def _identity(hwnd: int):
    from services.wheelhouse.overlay_focus_hooks import ForegroundIdentity

    return ForegroundIdentity(
        hwnd=hwnd, pid=hwnd * 10, process_name=f"w{hwnd}.exe",
        window_creation_time=hwnd * 100,
    )


def _machine_like(*, pinned, prior=None, deferred=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        pinned_snapshot_id=pinned,
        prior_pinned_snapshot_id=prior,
        prior_pin_deferred=deferred,
    )


def test_pin_effect_records_per_snapshot_window_identity():
    controller = _controller()
    _wire_app(controller, lambda a, p: _pin_response(a, p))
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value=_identity(1)
    )
    effect = Effect(
        kind=EffectKind.PIN_SNAPSHOT, overlay_session_id=7,
        paint_generation=0, snapshot_id="snap-w1",
    )
    asyncio.run(controller._dispatch_overlay_effects((effect,), trace_id="tr"))
    assert (controller._overlay_snapshot_window_identity["snap-w1"]
            == _identity(1))


def test_unpin_effect_drops_per_snapshot_window_identity():
    controller = _controller()
    _wire_app(controller, lambda a, p: _pin_response(a, p))
    controller._overlay_snapshot_window_identity["snap-w1"] = _identity(1)
    effect = Effect(
        kind=EffectKind.UNPIN_SNAPSHOT, overlay_session_id=7,
        snapshot_id="snap-w1",
    )
    asyncio.run(controller._dispatch_overlay_effects((effect,), trace_id="tr"))
    assert "snap-w1" not in controller._overlay_snapshot_window_identity


def test_refresh_visible_window_helper_false_on_window_mismatch():
    # The visible snapshot was pinned for window 1; the foreground is now
    # window 2 -> the visible window is NOT the foreground.
    controller = _controller()
    controller._overlay_snapshot_window_identity["snap-w1"] = _identity(1)
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value=_identity(2)
    )
    machine = _machine_like(pinned="snap-w1")
    assert (controller._overlay_refresh_visible_window_is_foreground(machine)
            is False)


def test_refresh_visible_window_helper_true_on_same_window():
    controller = _controller()
    controller._overlay_snapshot_window_identity["snap-w1"] = _identity(1)
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value=_identity(1)
    )
    machine = _machine_like(pinned="snap-w1")
    assert (controller._overlay_refresh_visible_window_is_foreground(machine)
            is True)


def test_refresh_visible_window_helper_uses_prior_when_deferred():
    # With a deferred prior, the VISIBLE snapshot is the prior, not the new pin.
    # The prior belongs to window 1; the foreground (the new window) is window 2.
    controller = _controller()
    controller._overlay_snapshot_window_identity["snap-prior"] = _identity(1)
    controller._overlay_snapshot_window_identity["snap-new"] = _identity(2)
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value=_identity(2)
    )
    machine = _machine_like(
        pinned="snap-new", prior="snap-prior", deferred=True,
    )
    # Resolved against the PRIOR (window 1) vs foreground (window 2) -> False,
    # even though the current pin's window DOES match the foreground.
    assert (controller._overlay_refresh_visible_window_is_foreground(machine)
            is False)


def test_refresh_visible_window_helper_none_when_unrecorded():
    controller = _controller()
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value=_identity(2)
    )
    machine = _machine_like(pinned="snap-unknown")
    assert (controller._overlay_refresh_visible_window_is_foreground(machine)
            is None)


def test_closed_clears_per_snapshot_window_identity_map():
    from services.wheelhouse.click_overlay_state import OverlayState

    controller = _controller()
    controller._overlay_snapshot_window_identity["snap-w1"] = _identity(1)
    controller.click_overlay_state = MagicMock()
    controller.click_overlay_state.state = OverlayState.CLOSED
    controller._reconcile_overlay_tracked_identity()
    assert controller._overlay_snapshot_window_identity == {}


def _pin_response(action, params) -> dict:
    from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

    return PinSnapshotResponse(
        status="ok",
        reason=None,
        overlay_session_id=int(params.get("overlay_session_id", 0)),
        snapshot_id=str(params.get("snapshot_id", "")),
        pinned=action == "pin_snapshot",
    ).to_dict()


def test_fire_notice_with_notice_forwards_via_forward_click_notice():
    from services.wheelhouse.shared.click_notice import ClickNoticeEvent

    controller = _controller()
    _wire_app(controller, lambda a, p: {})
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
    notice = ClickNoticeEvent(
        outcome="ambiguous", reason=None, matched_name=None,
        matched_names=("a", "b"), spoken_name="x", app_friendly_name="App",
        snapshot_id="snap-a", trace_id="tr",
    )
    effect = Effect(kind=EffectKind.FIRE_NOTICE, notice=notice)
    asyncio.run(
        controller._dispatch_overlay_effects((effect,), trace_id="tr")
    )
    cast(MagicMock, controller._forward_click_notice).assert_called_once()
    _, kwargs = cast(MagicMock, controller._forward_click_notice).call_args
    assert kwargs.get("outcome") == "ambiguous"


def test_fire_notice_with_none_fires_standalone_failure_notice():
    controller = _controller()
    _wire_app(controller, lambda a, p: {})
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
    effect = Effect(kind=EffectKind.FIRE_NOTICE, notice=None)
    asyncio.run(
        controller._dispatch_overlay_effects((effect,), trace_id="tr")
    )
    notice = cast(MagicMock, controller._forward_click_notice)
    notice.assert_called_once()
    _, kwargs = notice.call_args
    assert kwargs.get("outcome") == "execution_failed"


def test_arm_timer_then_fire_feeds_timeout_to_machine():
    """ARM_TIMER schedules a real timer that feeds TIMEOUT on fire."""

    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, lambda a, p: {})
        # A short ARM_TIMER so the test does not wait the real walk deadline.
        effect = Effect(
            kind=EffectKind.ARM_TIMER,
            overlay_session_id=sid,
            paint_generation=gen,
            timer_state=OverlayState.WALK_IN_FLIGHT,
            duration_ms=10.0,
        )
        await controller._dispatch_overlay_effects((effect,), trace_id="tr")
        # Let the timer fire and the resulting effects drain.
        await asyncio.sleep(0.05)
        await asyncio.gather(*controller.background_tasks)
        return controller

    controller = asyncio.run(_run())
    # walk_in_flight + TIMEOUT -> error -> closed (the machine's contract).
    assert machine.state is OverlayState.CLOSED


def test_cancel_timer_prevents_timeout_feed():
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, lambda a, p: {})
        arm = Effect(
            kind=EffectKind.ARM_TIMER,
            overlay_session_id=sid,
            paint_generation=gen,
            timer_state=OverlayState.WALK_IN_FLIGHT,
            duration_ms=10.0,
        )
        await controller._dispatch_overlay_effects((arm,), trace_id="tr")
        cancel = Effect(kind=EffectKind.CANCEL_TIMER)
        await controller._dispatch_overlay_effects((cancel,), trace_id="tr")
        await asyncio.sleep(0.05)
        return controller

    asyncio.run(_run())
    # The timer was cancelled before it could feed TIMEOUT, so the machine
    # stayed in walk_in_flight.
    assert machine.state is OverlayState.WALK_IN_FLIGHT


def test_end_to_end_show_numbers_reaches_painted():
    """RELEASE GATE: standalone 'show numbers' reaches PAINTED end to end.

    closed -> walk_in_flight -> [build_response] -> paint_in_flight ->
    [paint_ack] -> painted, with the build dispatched to Input (start_overlay_walk)
    and the paint dispatched to the GUI (paint_overlay).
    """

    async def _run():
        controller = _controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        machine = controller.click_overlay_state

        def _respond(action, params):
            sid = params["overlay_session_id"]
            gen = params["paint_generation"]
            if action == "start_overlay_walk":
                return _walk_response("snap-e2e", sid, gen, 1, 2, 3)
            return _pin_response(action, params)

        _wire_app(controller, _respond)

        # 'show numbers' -> walk_in_flight, build dispatched + summary cached +
        # BUILD_RESPONSE fed -> paint_in_flight, paint dispatched to GUI.
        await controller.handle_overlay_command("show", "tr-e2e")
        await _settle(controller)
        assert machine.state is OverlayState.PAINT_IN_FLIGHT

        sid, gen = machine.overlay_session_id, machine.paint_generation
        # GUI acks the paint -> painted.
        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        ack = OverlayStateChangedEvent(
            state="painted",
            overlay_session_id=sid,
            paint_generation=gen,
            monitor_ids=(0,),
            snapshot_id="snap-e2e",
        ).to_dict()
        await controller._handle_overlay_state_changed(ack)
        await _settle(controller)
        # PAINTED arms the periodic keepalive; cancel it so it does not outlive
        # the test loop.
        controller._overlay_cancel_keepalive_timer()
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.PAINTED
    actions = [m.get("action") for m in _gui_items(controller)]
    assert "paint_overlay" in actions
    # The build was dispatched to Input.
    sent_actions = [a for a, _ in controller._sent]  # type: ignore[attr-defined]
    assert "start_overlay_walk" in sent_actions


def test_effect_dispatch_is_ordered_under_concurrent_batches():
    """Concurrent _perform_overlay_effects batches dispatch in order.

    A not-yet-completed clear from one batch must not be reordered against a
    paint/clear from a later batch: the asyncio.Lock serializes whole batches
    in scheduling order.
    """

    async def _run():
        controller = _controller()
        controller.loop = asyncio.get_running_loop()
        order: list[str] = []

        async def _slow_clear(_effect, _trace):
            await asyncio.sleep(0.02)
            order.append("clear-A")

        # Patch the per-effect clear so batch A's clear is slow; if the lock
        # is missing, batch B's clear would land first.
        async def _fast_clear(_effect, _trace):
            order.append("clear-B")

        clears = {"a": _slow_clear, "b": _fast_clear}

        async def _batch(which):
            async def _do_clear(_e, _t):
                await clears[which](_e, _t)
            # Drive through the real lock-guarded dispatcher with one CLEAR.
            controller._overlay_dispatch_clear_one = _do_clear  # type: ignore[attr-defined]
            await controller._dispatch_overlay_effects(
                (Effect(kind=EffectKind.DISPATCH_CLEAR),), trace_id=which,
            )

        # Schedule batch A then batch B; A holds the lock through its slow clear.
        t_a = asyncio.ensure_future(_batch("a"))
        await asyncio.sleep(0)  # let A acquire the lock first
        t_b = asyncio.ensure_future(_batch("b"))
        await asyncio.gather(t_a, t_b)
        return order

    order = asyncio.run(_run())
    assert order == ["clear-A", "clear-B"]


# ---------------------------------------------------------------------------
# PART 3: the 200ms hold-or-drop timer.
# ---------------------------------------------------------------------------


def test_hold_resolves_to_snapshot_item_when_painted_after_hold():
    """A held 'click N' that becomes resolvable when the hold fires clicks."""

    async def _run():
        machine = ClickOverlayStateMachine()
        machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        # Stay walk_in_flight so the click is HELD; the hold timer re-reads.
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        cache = controller.click_snapshot_summary_cache
        captured = {}

        def _dispatch(*, snapshot_id, item_id, trace_id, gesture=None):
            captured["item_id"] = item_id

        controller._dispatch_snapshot_item_click = _dispatch  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        controller._hold_click_n(number=2, spoken="2", trace_id="tr-h")
        # While the hold is pending, the overlay paints: machine -> painted and
        # the cache has the visible snapshot.
        sid, gen = machine.overlay_session_id, machine.paint_generation
        machine.apply(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-h",
            )
        )
        machine.apply(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            )
        )
        cache.put("snap-h", _summary("snap-h", 1, 2, 3))
        # Wait past the 200ms hold.
        await asyncio.sleep(0.25)
        await asyncio.gather(*controller.background_tasks)
        return captured, controller

    captured, controller = asyncio.run(_run())
    assert captured.get("item_id") == "snap-h-item-2"
    cast(MagicMock, controller._forward_click_notice).assert_not_called()


def test_hold_expiry_with_no_painted_snapshot_fires_notice():
    """Criterion 1: held click never silently dropped; on expiry with no
    painted snapshot the 'show numbers did not paint yet' notice fires."""

    async def _run():
        machine = ClickOverlayStateMachine()
        machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        # Stay walk_in_flight: the build never resolves, so on hold expiry the
        # machine is still not painted.
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._dispatch_snapshot_item_click = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        controller._hold_click_n(number=2, spoken="2", trace_id="tr-h2")
        await asyncio.sleep(0.25)
        await asyncio.gather(*controller.background_tasks)
        return controller

    controller = asyncio.run(_run())
    cast(MagicMock, controller._dispatch_snapshot_item_click).assert_not_called()
    notice = cast(MagicMock, controller._forward_click_notice)
    notice.assert_called_once()
    _, kwargs = notice.call_args
    assert kwargs.get("outcome") == "execution_failed"
    assert kwargs.get("reason") == "numbers_not_showing"


# ---------------------------------------------------------------------------
# PART 4: summary-cache keepalive past the 30s TTL for a visible overlay.
# ---------------------------------------------------------------------------


def test_visible_overlay_snapshot_kept_alive_past_ttl():
    """A visible snapshot's TTL window is reset on each pin/paint access.

    With a tiny TTL and a controllable clock, a snapshot that the overlay keeps
    visible is re-put by the keepalive on each pin/paint access WHILE it is still
    alive, resetting the TTL window. The result is that a 'click N' against a
    still-visible overlay -- even one that has been on screen longer than the raw
    TTL -- still resolves, instead of misreporting 'no badge N' (criterion 4).
    """

    clock = {"t": 0.0}
    cache = ClickSnapshotSummaryCache(
        ttl_seconds=1.0, time_source=lambda: clock["t"]
    )
    cache.put("snap-k", _summary("snap-k", 1, 2))
    controller = _controller(cache=cache)
    _wire_app(controller, lambda a, p: _pin_response(a, p))
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value=None
    )
    effect = Effect(
        kind=EffectKind.PIN_SNAPSHOT,
        overlay_session_id=1,
        paint_generation=0,
        snapshot_id="snap-k",
    )

    from services.wheelhouse.click_snapshot_summary_cache import (
        resolve_display_number,
        ResolveOutcome,
    )

    # Advance to just before the raw TTL boundary and re-put (keepalive while the
    # entry is still alive), then advance again past the ORIGINAL TTL boundary
    # but within the re-put window. Total elapsed (1.4s) exceeds the 1.0s raw
    # TTL, yet the snapshot is still resolvable because each access reset it.
    clock["t"] = 0.9
    asyncio.run(controller._dispatch_overlay_effects((effect,), trace_id="tr"))
    clock["t"] = 1.4  # > original 1.0 TTL, but only 0.5 since the re-put
    result = resolve_display_number(cache, "snap-k", 2)
    assert result.outcome is ResolveOutcome.FOUND
    assert result.item_id == "snap-k-item-2"

    # A control without the keepalive would have expired: a snapshot put once at
    # t=0 and never re-accessed is gone by t=1.4.
    cache.put("snap-stale", _summary("snap-stale", 1))
    cache._entries["snap-stale"] = (  # type: ignore[attr-defined]
        cache._entries["snap-stale"][0], 0.0,  # type: ignore[attr-defined]
    )
    assert (
        resolve_display_number(cache, "snap-stale", 1).outcome
        is ResolveOutcome.SNAPSHOT_EXPIRED
    )


# ---------------------------------------------------------------------------
# PART 5: tracked identity cleared on entry to closed.
# ---------------------------------------------------------------------------


def test_tracked_identity_cleared_on_entry_to_closed_via_hide():
    """hide_numbers reaches closed and clears _overlay_tracked_identity."""

    async def _run():
        controller = _controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        machine = controller.click_overlay_state

        def _respond(action, params):
            sid = params.get("overlay_session_id", 0)
            gen = params.get("paint_generation", 0)
            if action == "start_overlay_walk":
                return _walk_response("snap-id", sid, gen, 1)
            return _pin_response(action, params)

        _wire_app(controller, _respond)
        controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
            return_value="ID"
        )
        await controller.handle_overlay_command("show", "tr")
        await _settle(controller)
        # The pin assigned the tracked identity.
        assert controller._overlay_tracked_identity == "ID"

        # hide -> closed -> identity cleared.
        await controller.handle_overlay_command("hide", "tr2")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    assert controller._overlay_tracked_identity is None


# ---------------------------------------------------------------------------
# FINDING 2 (wh-n29v.96.2): keepalive must slide for a quiescent painted
# overlay. PAINTED is a steady NO_TIMEOUT state with no recurring pin/paint, so
# a periodic keepalive timer must re-put the visible summary while painted, or a
# >TTL idle dwell loses the summary and 'click N' misreports 'no badge N'.
# ---------------------------------------------------------------------------


def test_quiescent_painted_overlay_kept_alive_past_ttl():
    """A painted overlay idle PAST the TTL with no interaction still resolves.

    Drive to PAINTED, advance the injected clock past the TTL with the periodic
    keepalive timer firing (re-put each ~TTL/2), then resolve 'click N' and
    assert it does NOT misreport SNAPSHOT_EXPIRED.
    """

    async def _run():
        clock = {"t": 0.0}
        cache = ClickSnapshotSummaryCache(
            ttl_seconds=1.0, time_source=lambda: clock["t"]
        )
        controller = _controller(cache=cache)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        machine = controller.click_overlay_state

        def _respond(action, params):
            sid = params.get("overlay_session_id", 0)
            gen = params.get("paint_generation", 0)
            if action == "start_overlay_walk":
                return _walk_response("snap-q", sid, gen, 1, 2, 3)
            return _pin_response(action, params)

        _wire_app(controller, _respond)
        controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
            return_value=None
        )
        await controller.handle_overlay_command("show", "tr-q")
        await _settle(controller)
        sid, gen = machine.overlay_session_id, machine.paint_generation
        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        ack = OverlayStateChangedEvent(
            state="painted", overlay_session_id=sid, paint_generation=gen,
            monitor_ids=(0,), snapshot_id="snap-q",
        ).to_dict()
        await controller._handle_overlay_state_changed(ack)
        await _settle(controller)
        assert machine.state is OverlayState.PAINTED
        # PAINTED armed the periodic keepalive (a real loop.call_later); cancel
        # it so the deterministic manual drive below is the only re-put source.
        assert controller._overlay_keepalive_timer is not None
        controller._overlay_cancel_keepalive_timer()

        # Idle for longer than the TTL. Drive the periodic keepalive callback
        # directly between injected-clock advances so each re-put lands inside
        # the window; total elapsed (3.2s) far exceeds the 1.0s raw TTL, yet the
        # snapshot stays resolvable because the keepalive slides it.
        for _ in range(8):
            clock["t"] += 0.4  # < 1.0 TTL per step
            controller._fire_overlay_keepalive()  # re-put + reschedule
            controller._overlay_cancel_keepalive_timer()  # drop the reschedule
        assert clock["t"] > 1.0  # well past the raw TTL
        return controller, cache

    controller, cache = asyncio.run(_run())
    from services.wheelhouse.click_snapshot_summary_cache import (
        resolve_display_number,
        ResolveOutcome,
    )

    result = resolve_display_number(cache, "snap-q", 3)
    assert result.outcome is ResolveOutcome.FOUND
    assert result.item_id == "snap-q-item-3"


def test_keepalive_sends_input_store_refresh_for_pinned_snapshot():
    """The 15s keepalive slides the INPUT store's TTL too, not just the Logic
    cache (wh-overlay-snapshot-keepalive trigger A).

    The Logic resolver cache and the Input ElementFinder store both expire 30s
    after the walk. Before this fix the keepalive re-put only the Logic cache, so
    the Input copy aged out while Logic kept resolving and dispatching "click N"
    -- the click then failed with snapshot_expired on a still-visible overlay.
    The keepalive must now ALSO send refresh_overlay_snapshot to Input for the
    pinned snapshot on each tick.
    """

    async def _run():
        controller = _controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        machine = controller.click_overlay_state

        def _respond(action, params):
            sid = params.get("overlay_session_id", 0)
            gen = params.get("paint_generation", 0)
            if action == "start_overlay_walk":
                return _walk_response("snap-k", sid, gen, 1, 2)
            return _pin_response(action, params)

        sent = _wire_app(controller, _respond)
        controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
            return_value="ID"
        )
        await controller.handle_overlay_command("show", "tr-k")
        await _settle(controller)
        sid, gen = machine.overlay_session_id, machine.paint_generation
        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        ack = OverlayStateChangedEvent(
            state="painted", overlay_session_id=sid, paint_generation=gen,
            monitor_ids=(0,), snapshot_id="snap-k",
        ).to_dict()
        await controller._handle_overlay_state_changed(ack)
        await _settle(controller)
        assert machine.state is OverlayState.PAINTED
        assert machine.pinned_snapshot_id == "snap-k"
        # Drop the auto-armed real timer and ignore the show/paint sends; only
        # the keepalive tick's sends matter below.
        controller._overlay_cancel_keepalive_timer()
        sent.clear()

        controller._fire_overlay_keepalive()
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()  # drop the reschedule
        return sent

    sent = asyncio.run(_run())
    refresh = [params for (action, params) in sent
               if action == "refresh_overlay_snapshot"]
    assert len(refresh) == 1
    assert refresh[0]["snapshot_id"] == "snap-k"


def test_keepalive_timer_cancelled_on_leaving_painted():
    """The periodic keepalive timer is armed in PAINTED and cancelled on hide."""

    async def _run():
        controller = _controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        machine = controller.click_overlay_state

        def _respond(action, params):
            sid = params.get("overlay_session_id", 0)
            gen = params.get("paint_generation", 0)
            if action == "start_overlay_walk":
                return _walk_response("snap-c", sid, gen, 1)
            return _pin_response(action, params)

        _wire_app(controller, _respond)
        controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
            return_value=None
        )
        await controller.handle_overlay_command("show", "tr")
        await _settle(controller)
        sid, gen = machine.overlay_session_id, machine.paint_generation
        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        ack = OverlayStateChangedEvent(
            state="painted", overlay_session_id=sid, paint_generation=gen,
            monitor_ids=(0,), snapshot_id="snap-c",
        ).to_dict()
        await controller._handle_overlay_state_changed(ack)
        await _settle(controller)
        assert machine.state is OverlayState.PAINTED
        assert controller._overlay_keepalive_timer is not None

        await controller.handle_overlay_command("hide", "tr2")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    # Leaving PAINTED cancelled the periodic keepalive (no leak).
    assert controller._overlay_keepalive_timer is None


# ---------------------------------------------------------------------------
# FINDING 3 (wh-n29v.96.3): a held 'click N' must not hit the wrong overlay.
# The held click carries the (session, generation) armed at hold time; if a
# supersede / new session reaches a resolvable state within the 200ms hold, the
# armed pair no longer matches the machine and the click must NOT fire.
# ---------------------------------------------------------------------------


def test_held_click_n_rejects_on_generation_mismatch_after_supersede():
    """A supersede within the hold makes the held click resolve against a
    snapshot the user never saw -> reject with numbers_not_showing, no click."""

    async def _run():
        machine = ClickOverlayStateMachine()
        machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        cache = controller.click_snapshot_summary_cache
        controller._dispatch_snapshot_item_click = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        # Arm the hold at the CURRENT (sid, g).
        controller._hold_click_n(number=2, spoken="2", trace_id="tr-h3")

        # A supersede (FOCUS_CHANGE in walk_in_flight) bumps the generation to a
        # NEW walk, which then reaches PAINTED with a DIFFERENT snapshot.
        machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
        sid, gen = machine.overlay_session_id, machine.paint_generation
        machine.apply(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-new",
            )
        )
        machine.apply(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            )
        )
        cache.put("snap-new", _summary("snap-new", 1, 2, 3))
        assert machine.state is OverlayState.PAINTED

        await asyncio.sleep(0.25)
        await asyncio.gather(*controller.background_tasks)
        return controller

    controller = asyncio.run(_run())
    # The held click was armed at the OLD generation; the machine has since
    # superseded, so the click must NOT fire against the new snapshot.
    cast(MagicMock, controller._dispatch_snapshot_item_click).assert_not_called()
    notice = cast(MagicMock, controller._forward_click_notice)
    notice.assert_called_once()
    _, kwargs = notice.call_args
    assert kwargs.get("reason") == "numbers_not_showing"


def test_held_click_n_resolves_when_generation_matches():
    """Positive control: same (session, generation) at hold-fire -> the click
    dispatches (the Finding 3 gate does not over-reject the in-session case)."""

    async def _run():
        machine = ClickOverlayStateMachine()
        machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        cache = controller.click_snapshot_summary_cache
        captured = {}

        def _dispatch(*, snapshot_id, item_id, trace_id, gesture=None):
            captured["item_id"] = item_id

        controller._dispatch_snapshot_item_click = _dispatch  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        controller._hold_click_n(number=2, spoken="2", trace_id="tr-ok")
        # SAME session/generation reaches painted (no supersede).
        sid, gen = machine.overlay_session_id, machine.paint_generation
        machine.apply(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-same",
            )
        )
        machine.apply(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            )
        )
        cache.put("snap-same", _summary("snap-same", 1, 2, 3))
        await asyncio.sleep(0.25)
        await asyncio.gather(*controller.background_tasks)
        return controller, captured

    controller, captured = asyncio.run(_run())
    assert captured.get("item_id") == "snap-same-item-2"
    cast(MagicMock, controller._forward_click_notice).assert_not_called()


# ---------------------------------------------------------------------------
# FINDING 4 (wh-n29v.96.4): the synchronous BUILD_RESPONSE feed must be deferred
# so the in-flight walk batch fully drains (including its trailing ARM_TIMER)
# and releases the lock BEFORE the build-response transition schedules its
# effects. Otherwise a stale WALK-duration timer is briefly armed at the
# still-current generation.
# ---------------------------------------------------------------------------


def test_build_response_feed_does_not_leave_stale_walk_timer():
    """After a synchronous build-response feed, the live timer is the PAINT
    timer, not a stale WALK timer."""

    async def _run():
        machine = ClickOverlayStateMachine()
        machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        sid, gen = machine.overlay_session_id, machine.paint_generation
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        def _respond(action, params):
            s = params.get("overlay_session_id", 0)
            g = params.get("paint_generation", 0)
            if action == "start_overlay_walk":
                return _walk_response("snap-f4", s, g, 1, 2)
            return _pin_response(action, params)

        _wire_app(controller, _respond)

        # Dispatch the SHOW_NUMBERS effects (DISPATCH_BUILD + ARM_TIMER(WALK)).
        # The build awaiter feeds BUILD_RESPONSE; the fix defers that feed so the
        # walk batch's trailing ARM_TIMER(WALK) is processed and then the
        # build-response transition (-> paint_in_flight) re-arms a PAINT timer.
        effects = (
            Effect(
                kind=EffectKind.DISPATCH_BUILD, overlay_session_id=sid,
                paint_generation=gen, build_reason=BuildReason.SHOW_NUMBERS,
            ),
            Effect(
                kind=EffectKind.ARM_TIMER, overlay_session_id=sid,
                paint_generation=gen, timer_state=OverlayState.WALK_IN_FLIGHT,
                duration_ms=machine.walk_deadline_ms,
            ),
        )
        await controller._dispatch_overlay_effects(effects, trace_id="tr-f4")
        # Let the deferred feed + the resulting paint_in_flight batch run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.gather(*controller.background_tasks)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.PAINT_IN_FLIGHT
    # The surviving timer is the PAINT-duration timer, not a stale WALK timer.
    assert controller._overlay_timer_pair == (
        machine.overlay_session_id, machine.paint_generation,
    )
    assert controller._overlay_armed_timer_state is OverlayState.PAINT_IN_FLIGHT
    # Clean up the live timer.
    controller._overlay_cancel_timer()


# ---------------------------------------------------------------------------
# reviewer_1 (wh-n29v.97) findings .2 and .3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snapshot_id,item_id",
    [
        (None, "snap-item-2"),
        ("snap", None),
        ("", "snap-item-2"),
        ("snap", ""),
    ],
)
def test_dispatch_snapshot_item_click_missing_identity_surfaces_notice(
    snapshot_id, item_id,
):
    """Finding wh-n29v.97.2: a missing snapshot_id/item_id must NOT drop the
    click silently. The slice's own 'never a silent drop' principle applies to
    an invariant violation too: surface an execution_failed notice so a
    hands-free user gets feedback, and do NOT schedule an Input send."""

    controller = _controller()
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
    controller.create_task_with_error_handling = MagicMock()  # type: ignore[method-assign]

    controller._dispatch_snapshot_item_click(
        snapshot_id=snapshot_id, item_id=item_id, trace_id="tr-mi",
    )

    # No Input round trip is scheduled for an unusable identity.
    cast(
        MagicMock, controller.create_task_with_error_handling
    ).assert_not_called()
    notice = cast(MagicMock, controller._forward_click_notice)
    notice.assert_called_once()
    _, kwargs = notice.call_args
    assert kwargs.get("outcome") == "execution_failed"
    assert kwargs.get("reason") == "invalid_snapshot_item"
    assert kwargs.get("trace_id") == "tr-mi"


def test_dispatch_build_echoed_generation_mismatch_treated_as_failure():
    """Finding wh-n29v.97.3: the build response echoes (overlay_session_id,
    paint_generation) 'for the generation/supersession check' (v4 design line
    186). A response whose echoed pair does NOT equal the request pair is not
    trustworthy; Logic must treat it as a build failure (do NOT paint, do NOT
    cache the suspect snapshot) rather than restamping it with the request pair
    and letting it through the stale-generation gate."""

    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        def _respond(action, params):
            if action == "start_overlay_walk":
                # Echo a DIFFERENT generation than the request pair.
                return _walk_response("snap-skew", sid, gen + 7, 1, 2)
            return _pin_response(action, params)

        _wire_app(controller, _respond)
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SHOW_NUMBERS,
        )
        await controller._dispatch_overlay_effects((effect,), trace_id="tr-sk")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    # The skewed-generation response is treated as a build failure: the machine
    # does NOT advance to a painted-bound state and the suspect snapshot is not
    # cached.
    assert machine.state is not OverlayState.PAINT_IN_FLIGHT
    assert (
        controller.click_snapshot_summary_cache.resolve("snap-skew").summary
        is None
    )


def test_keepalive_not_armed_for_empty_pinned_snapshot():
    """Finding wh-n29v.98.3: the keepalive-arm guard must use a truthy check on
    pinned_snapshot_id, matching the sibling _overlay_keepalive_summary early
    return (`if not snapshot_id`). An empty-string pin must NOT arm a periodic
    keepalive timer that would fire forever as a no-op."""

    async def _run():
        controller = _controller()
        controller.loop = asyncio.get_running_loop()
        machine = MagicMock()
        machine.state = OverlayState.PAINTED
        machine.pinned_snapshot_id = ""
        controller.click_overlay_state = machine
        controller._overlay_keepalive_timer = None
        controller._reconcile_overlay_keepalive_timer()
        return controller

    controller = asyncio.run(_run())
    assert controller._overlay_keepalive_timer is None


# ---------------------------------------------------------------------------
# Post-click refresh feed (this slice): a successful numbered-overlay item
# click must feed an OverlayEvent(CLICK_COMPLETE) so a painted overlay
# refreshes (painted -> refresh_in_flight, generation bumped) instead of
# staying pinned to the pre-click UI. The state machine already implements
# painted + CLICK_COMPLETE -> _refresh; this verifies the Logic side feeds it.
# ---------------------------------------------------------------------------


def _drive_to_painted(machine, snapshot_id: str):
    """Drive a fresh machine to PAINTED purely (no IPC) and return (sid, gen).

    closed -> walk_in_flight -> [build_response] -> paint_in_flight ->
    [paint_ack painted] -> painted. Mirrors the direct ``machine.apply`` setup
    the supersede / refresh tests above use, bypassing the cross-process build.
    """

    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation
    machine.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
            paint_generation=gen, snapshot_id=snapshot_id,
        )
    )
    machine.apply(
        OverlayEvent(
            OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
            paint_generation=gen, paint_state=PaintAckState.PAINTED,
        )
    )
    assert machine.state is OverlayState.PAINTED
    return sid, gen


def test_ok_click_feeds_click_complete_and_painted_refreshes():
    """A successful click_snapshot_item feeds CLICK_COMPLETE; a painted overlay
    transitions painted -> refresh_in_flight with a bumped generation."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-pc")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        # Don't perform the refresh's real IPC effects; this test asserts the
        # machine transition the CLICK_COMPLETE feed drives, not the build send.
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-pc", item_id="snap-pc-item-2", trace_id="tr-pc",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    # The ok branch must NOT forward a click-notice (only non-ok does).
    cast(MagicMock, controller._forward_click_notice).assert_not_called()
    # painted + CLICK_COMPLETE -> refresh_in_flight, generation bumped.
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert machine.overlay_session_id == sid
    assert machine.paint_generation == gen + 1


def test_taskbar_closed_is_a_rewalk_refusal_reason():
    """taskbar_closed is a position-staleness refusal exactly like
    popup_closed: the shell window the badge pointed at is gone (or its
    HWND was recycled), so the pinned snapshot is stale and every retry
    against it fails identically. It must trigger the overlay re-walk.
    A DIRECT literal assertion, not derived from the set, because the
    parametrized test below cannot catch an omission from the set itself
    (codex finding wh-overlay-taskbar-numbers.5.4)."""
    assert "taskbar_closed" in _OVERLAY_REWALK_REFUSAL_REASONS


@pytest.mark.parametrize("reason", sorted(_OVERLAY_REWALK_REFUSAL_REASONS))
def test_stale_position_refusal_feeds_click_complete_and_painted_refreshes(reason):
    """Every position-staleness refusal MUST feed CLICK_COMPLETE so the overlay
    re-walks and repaints. Parametrized from _OVERLAY_REWALK_REFUSAL_REASONS so a
    new reason added to that set (the executor's bounds_invalid, bounds_stale,
    target_moved_offscreen, popup_closed) is automatically covered here. Without
    the re-walk the stale snapshot stays pinned and every retry against it fails
    the same way (wh-overlay-stale-click-refresh). The user is still told via the
    notice. target_moved_offscreen matters most: a control scrolled out of view
    in a browser reports THAT reason (the off-screen check precedes the drift
    check in click_executor._verify), not bounds_stale (reviewer_0 finding)."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-stale")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(
            controller,
            lambda a, p: _click_response("execution_failed", reason=reason),
        )
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-stale", item_id="snap-stale-item-2",
            trace_id="tr-stale", overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    # The user is still told the click did not land.
    cast(MagicMock, controller._forward_click_notice).assert_called_once()
    # AND the overlay re-walks: painted + CLICK_COMPLETE -> refresh_in_flight.
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert machine.overlay_session_id == sid
    assert machine.paint_generation == gen + 1


@pytest.mark.parametrize(
    "reason", ["invoke_com_error", "item_not_found", "disabled"]
)
def test_non_position_refusal_notices_but_does_not_refresh(reason):
    """A refusal that is NOT a position-staleness signal (the control did not
    respond to Invoke, the item was missing, or the control is disabled in place)
    forwards the notice but does NOT re-walk: a fresh walk finds the same control
    in the same place, so it would not change the outcome and would only churn
    the overlay (wh-overlay-stale-click-refresh). None of these reasons are in
    _OVERLAY_REWALK_REFUSAL_REASONS."""
    assert reason not in _OVERLAY_REWALK_REFUSAL_REASONS

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-keep")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(
            controller,
            lambda a, p: _click_response("execution_failed", reason=reason),
        )
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-keep", item_id="snap-keep-item-2",
            trace_id="tr-keep", overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    cast(MagicMock, controller._forward_click_notice).assert_called_once()
    # No re-walk: the overlay stays painted at the same generation.
    assert machine.state is OverlayState.PAINTED
    assert machine.paint_generation == gen


def test_ok_click_for_superseded_generation_does_not_refresh():
    """A CLICK_COMPLETE for a click dispatched against a superseded generation
    must NOT refresh the newer overlay -- the machine stays PAINTED."""

    async def _run():
        machine = ClickOverlayStateMachine()
        # The click was dispatched against this (old) painted overlay.
        old_sid, old_gen = _drive_to_painted(machine, "snap-old")
        # A focus-change supersede bumps the generation; a NEW painted overlay
        # is now visible at a DIFFERENT generation than the one the click saw.
        machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
        new_sid, new_gen = machine.overlay_session_id, machine.paint_generation
        machine.apply(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=new_sid,
                paint_generation=new_gen, snapshot_id="snap-new",
            )
        )
        machine.apply(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=new_sid,
                paint_generation=new_gen, paint_state=PaintAckState.PAINTED,
            )
        )
        assert machine.state is OverlayState.PAINTED
        assert (new_sid, new_gen) != (old_sid, old_gen)

        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        # The click carries the OLD pair it was dispatched against.
        await controller._send_snapshot_item_click(
            snapshot_id="snap-old", item_id="snap-old-item-2",
            trace_id="tr-sup", overlay_dispatch_pair=(old_sid, old_gen),
        )
        await asyncio.sleep(0)
        return machine, (new_sid, new_gen)

    machine, (new_sid, new_gen) = asyncio.run(_run())
    # The superseded click did NOT refresh the newer overlay.
    assert machine.state is OverlayState.PAINTED
    assert machine.overlay_session_id == new_sid
    assert machine.paint_generation == new_gen


def test_ok_click_when_machine_closed_is_noop_and_does_not_raise():
    """A CLICK_COMPLETE arriving when the machine is closed must remain a no-op
    and must not raise (preserves wh-n29v.95 part 6)."""

    async def _run():
        machine = ClickOverlayStateMachine()
        # The click was dispatched against a painted overlay that has since been
        # torn down (hide -> closed). The machine is now CLOSED.
        sid, gen = _drive_to_painted(machine, "snap-c")
        machine.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
        assert machine.state is OverlayState.CLOSED

        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-c", item_id="snap-c-item-2", trace_id="tr-cl",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        return machine

    machine = asyncio.run(_run())
    # No error, no refresh: the machine is unchanged in CLOSED.
    assert machine.state is OverlayState.CLOSED


def test_ok_click_when_paused_does_not_drive_the_machine_to_error():
    """A CLICK_COMPLETE for a click dispatched at the current pair while the
    machine is now PAUSED must never reach the machine (which would take
    ``_on_paused``'s _invalid path to error). The state-required gate, not
    pair-match alone, keeps a same-generation paused overlay out of error.
    The refresh itself is deferred, not lost -- see
    ``test_ok_click_while_paused_defers_the_postclick_refresh``
    (wh-overlay-slow-uia-stale-badges.21.2)."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-p")
        # painted -> paused via mic pause does NOT bump the generation, so the
        # dispatch pair still matches; only the PAINTED-state gate prevents the
        # feed from driving the machine's _on_paused(_invalid) -> error path.
        machine.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
        assert machine.state is OverlayState.PAUSED
        assert (machine.overlay_session_id, machine.paint_generation) == (
            sid, gen,
        )

        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-p", item_id="snap-p-item-2", trace_id="tr-pa",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        return machine

    machine = asyncio.run(_run())
    # The paused overlay was not driven to error by a same-pair CLICK_COMPLETE.
    assert machine.state is OverlayState.PAUSED


def test_dispatch_snapshot_item_click_threads_overlay_pair_to_send():
    """The synchronous dispatch seam captures the machine's current
    (overlay_session_id, paint_generation) and threads it to the async send so
    the post-click CLICK_COMPLETE feed can gate on a superseded/torn-down click."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-th")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        captured = {}

        async def _fake_send(*, snapshot_id, item_id, trace_id,
                              overlay_dispatch_pair,
                              overlay_resolved_pair=None, gesture=None):
            captured["pair"] = overlay_dispatch_pair

        controller._send_snapshot_item_click = _fake_send  # type: ignore[method-assign]
        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-th", item_id="snap-th-item-2", trace_id="tr-th",
        )
        await asyncio.sleep(0)
        await asyncio.gather(*controller.background_tasks)
        return captured, (sid, gen)

    captured, pair = asyncio.run(_run())
    assert captured["pair"] == pair


# ---------------------------------------------------------------------------
# Post-click refresh deferral (wh-n29v.101.1): a click ok that arrives while the
# overlay is REFRESH_IN_FLIGHT cannot refresh immediately (the machine returns
# HELD for CLICK_COMPLETE in that state, and the captured dispatch pair already
# names the in-flight generation). The Logic side must record a PENDING
# post-click refresh keyed on that pair and replay CLICK_COMPLETE once the SAME
# generation settles into PAINTED, so the next 'click N' resolves against a
# fresh post-click snapshot rather than the pre-click one. The pending refresh
# is cleared on supersede (the live pair moves past the recorded one) and on
# session end (entry to closed) so a stale pending refresh never fires on a
# newer overlay.
# ---------------------------------------------------------------------------


def _drive_to_refresh_in_flight(machine, snapshot_id: str):
    """Drive a fresh machine to REFRESH_IN_FLIGHT at the bumped generation.

    closed -> ... -> painted(snapshot_id) -> [FOCUS_CHANGE] -> refresh_in_flight.
    The prior snapshot stays pinned/visible; the refresh build is in flight.
    Returns the (sid, gen) of the in-flight refresh generation -- the pair a
    'click N' dispatched now captures (``_dispatch_snapshot_item_click`` reads
    the machine's CURRENT pair, which in REFRESH_IN_FLIGHT is the already-bumped
    generation).
    """

    _drive_to_painted(machine, snapshot_id)
    machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    return machine.overlay_session_id, machine.paint_generation


def test_ok_click_during_refresh_in_flight_defers_refresh_until_painted():
    """The wh-n29v.101.1 fix: a click ok that arrives during REFRESH_IN_FLIGHT
    records a pending post-click refresh and replays CLICK_COMPLETE once THAT
    generation reaches PAINTED, so the refresh is deferred -- never dropped."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        # The click resolved against the still-visible pre-click overlay; its
        # dispatch pair is the already-bumped in-flight refresh generation.
        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-rif",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        # The refresh is deferred, not fired yet, and not dropped: the pending
        # pair is recorded and the machine is unchanged (still REFRESH_IN_FLIGHT
        # at the same generation).
        assert machine.state is OverlayState.REFRESH_IN_FLIGHT
        assert machine.paint_generation == gen
        assert controller._overlay_pending_postclick_refresh == (sid, gen)

        # The in-flight refresh now settles to PAINTED at the SAME generation,
        # driven through the integration path so the reconcile runs.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-B",
            ),
            source="test-build",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            ),
            source="test-paintack",
        )
        # The consume is deferred via call_soon; pump the loop so it runs.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    # The pending refresh was consumed: painted + CLICK_COMPLETE -> a fresh
    # post-click refresh at the NEXT generation.
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert machine.overlay_session_id == sid
    assert machine.paint_generation == gen + 1
    assert controller._overlay_pending_postclick_refresh is None


def test_pending_postclick_refresh_cleared_on_supersede():
    """A pending post-click refresh recorded during REFRESH_IN_FLIGHT is dropped
    when a focus-change supersede bumps the generation before the recorded
    generation reaches PAINTED, so the stale refresh never fires on the newer
    overlay."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-sup2",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_refresh == (sid, gen)

        # A focus-change supersede bumps to gen+1 (through the integration path
        # so the reconcile runs), abandoning the recorded generation.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-sup",
        )
        assert machine.paint_generation == gen + 1
        # The pending refresh keyed on the OLD pair is cleared by the reconcile.
        assert controller._overlay_pending_postclick_refresh is None

        # Drive the NEW generation to PAINTED; no extra post-click refresh fires.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, snapshot_id="snap-C",
            ),
            source="test-build2",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen + 1, paint_state=PaintAckState.PAINTED,
            ),
            source="test-pa2",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    assert machine.state is OverlayState.PAINTED
    assert machine.paint_generation == gen + 1
    assert controller._overlay_pending_postclick_refresh is None


def test_pending_postclick_refresh_cleared_on_session_end():
    """A pending post-click refresh is dropped when the overlay session ends
    (hide -> closed) before the recorded generation reaches PAINTED."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-end",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_refresh == (sid, gen)

        # 'hide numbers' tears the session down through handle_overlay_command,
        # which also reconciles the pending refresh on entry to closed.
        await controller.handle_overlay_command("hide", "tr-hide")
        assert machine.state is OverlayState.CLOSED
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    assert controller._overlay_pending_postclick_refresh is None


def test_pending_postclick_refresh_consumed_after_failed_refresh():
    """Even when the in-flight refresh FAILS (non-destructive fall-back to
    PAINTED at the same generation), the pending post-click refresh is consumed:
    the visible overlay is still the pre-click one, so a fresh post-click walk
    is still needed."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-fail",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_refresh == (sid, gen)

        # The in-flight refresh BUILD fails: the non-destructive fall-back keeps
        # the prior overlay and returns to PAINTED at the SAME generation.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id=None, build_ok=False,
            ),
            source="test-buildfail",
        )
        assert machine.state is OverlayState.PAINTED
        assert machine.paint_generation == gen
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    # The pre-click overlay was restored to PAINTED, so the pending refresh
    # still fires -> a fresh post-click refresh at the NEXT generation.
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert machine.paint_generation == gen + 1
    assert controller._overlay_pending_postclick_refresh is None


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.21.2: a click reply that lands while the
# machine is PAUSED at the matching dispatch pair must DEFER the post-click
# refresh, exactly like the REFRESH_IN_FLIGHT branch, not drop it. The pause
# lands between the click dispatch and its reply, so the refresh the click
# earned would otherwise disappear and the resume restore would repaint the
# PRE-CLICK list with no fresh walk behind it.
# ---------------------------------------------------------------------------


def test_ok_click_while_paused_defers_the_postclick_refresh():
    """A click ok that arrives while the machine is PAUSED at the matching pair
    records a pending post-click refresh instead of dropping it."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-pz")
        # painted -> paused via mic pause does NOT bump the generation, so the
        # click's dispatch pair still matches the machine's current pair.
        machine.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
        assert machine.state is OverlayState.PAUSED
        assert (machine.overlay_session_id, machine.paint_generation) == (
            sid, gen,
        )

        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-pz", item_id="snap-pz-item-2", trace_id="tr-pz",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    # The paused overlay is still not driven to error, and the refresh is
    # recorded for the resume rather than thrown away.
    assert machine.state is OverlayState.PAUSED
    assert controller._overlay_pending_postclick_refresh == (sid, gen)


def test_ok_click_while_paused_refresh_consumed_on_resume():
    """The deferred key recorded while PAUSED is one the resume can consume.

    The paused -> painted restore bumps the generation and re-keys the pending
    onto the restored pair (wh-overlay-slow-uia-stale-badges.17), so the
    reconciler consumes it there: a FRESH post-click walk runs instead of the
    consumed pre-click list staying on screen.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-pz2")
        machine.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
        assert machine.state is OverlayState.PAUSED

        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-pz2", item_id="snap-pz2-item-2",
            trace_id="tr-pz2", overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_refresh == (sid, gen)

        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True),
            source="test-resume-pz",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    # TWO bumps: the restore takes one so its repaint clears the GUI generation
    # gate, the consumed post-click refresh takes the next.
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert machine.overlay_session_id == sid
    assert machine.paint_generation == gen + 2
    assert controller._overlay_pending_postclick_refresh is None


def test_ok_click_after_session_teardown_records_no_pending_refresh():
    """CLOSED still DROPS the post-click refresh (the fix is PAUSED-only).

    Nothing is on screen and no overlay remains to refresh, so recording a
    pending pair there would leave a key the reconciler must drop anyway.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-cz")
        machine.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
        assert machine.state is OverlayState.CLOSED

        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-cz", item_id="snap-cz-item-2", trace_id="tr-cz",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    assert controller._overlay_pending_postclick_refresh is None


# ---------------------------------------------------------------------------
# Deferral robustness (wh-n29v.102.1 / .2 / .3): the pending pair is left SET
# at schedule time and the deferred callback owns the consume/keep/drop
# decision, so a mic-pause landing between the schedule and the callback fire
# cannot lose the refresh. These tests drive the machine's mic-pause/mic-resume
# transitions directly through _apply_overlay_event; the integration does not
# yet feed those events into the overlay machine (a later slice), so the PAUSED
# paths are exercised here to lock the deferral logic for when it does.
# ---------------------------------------------------------------------------


def test_pending_postclick_refresh_survives_pause_between_schedule_and_fire():
    """wh-n29v.102.1: a mic-pause processed AFTER the reconcile scheduled the
    deferred consume but BEFORE the callback fires must NOT lose the pending
    refresh; it consumes once the overlay resumes to PAINTED."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-pir",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_refresh == (sid, gen)

        # The refresh settles to PAINTED; the reconcile schedules the deferred
        # consume via call_soon. Do NOT pump the loop yet.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-B",
            ),
            source="test-build",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            ),
            source="test-pa",
        )
        assert machine.state is OverlayState.PAINTED

        # A mic-pause is processed BEFORE the call_soon callback fires:
        # painted -> paused at the SAME generation. The pending must survive.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_PAUSE), source="test-pause",
        )
        assert machine.state is OverlayState.PAUSED
        # Let the deferred callback fire; it must KEEP (not drop) the pending.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_refresh == (sid, gen)
        assert machine.state is OverlayState.PAUSED

        # Resume to PAINTED at the same pair -> the pending now consumes.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True),
            source="test-resume",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    # TWO bumps, not one (wh-overlay-slow-uia-stale-badges.17): the resume
    # restore takes a generation of its own so the GUI generation gate lets the
    # repaint present after the mic-pause clear, and the consumed post-click
    # refresh takes the next one. The behaviour under test is unchanged -- the
    # pending survived the pause and consumed on the resume; only the
    # arithmetic moved.
    assert machine.paint_generation == gen + 2
    assert controller._overlay_pending_postclick_refresh is None


def test_pending_postclick_refresh_kept_through_paused_then_consumed_on_resume():
    """wh-n29v.102.2: a mic-pause DURING the in-flight refresh (auto-hide) lands
    the overlay in PAUSED while a pending post-click refresh is recorded. The
    pending must survive the paused interval and consume only after resume to
    PAINTED."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-pkt",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_refresh == (sid, gen)

        # Mic pauses mid-refresh: auto-hide arms, the build paints + immediately
        # clears, and the paint-ack lands the overlay in PAUSED at the same gen.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_PAUSE), source="test-pause",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-B",
            ),
            source="test-build",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            ),
            source="test-pa",
        )
        assert machine.state is OverlayState.PAUSED
        await asyncio.sleep(0)
        # Still kept while paused.
        assert controller._overlay_pending_postclick_refresh == (sid, gen)

        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True),
            source="test-resume",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    # TWO bumps: the resume restore takes one so its repaint clears the GUI
    # generation gate, the consumed post-click refresh takes the next
    # (wh-overlay-slow-uia-stale-badges.17). See the sibling test above.
    assert machine.paint_generation == gen + 2
    assert controller._overlay_pending_postclick_refresh is None


def test_pending_postclick_refresh_dropped_when_the_resume_re_walks():
    """wh-overlay-slow-uia-stale-badges.17: the resume re-key is for the
    RESTORE leg only.

    A mic-resume whose cached snapshot is stale unpins and starts a fresh walk
    (PAUSED -> WALK_IN_FLIGHT). That walk supersedes the post-click refresh, so
    the pending must be DROPPED, not carried onto the walk's generation."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-prw",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_refresh == (sid, gen)

        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_PAUSE), source="test-pause",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-B",
            ),
            source="test-build",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            ),
            source="test-pa",
        )
        assert machine.state is OverlayState.PAUSED
        assert controller._overlay_pending_postclick_refresh == (sid, gen)

        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=False),
            source="test-resume-stale",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    assert machine.state is OverlayState.WALK_IN_FLIGHT
    assert controller._overlay_pending_postclick_refresh is None


def test_pending_postclick_refresh_callback_skips_after_supersede():
    """wh-n29v.102.3: if a focus-change supersede bumps the generation AFTER the
    reconcile scheduled the deferred consume but BEFORE the callback fires, the
    pending is dropped and the stale callback must not drive a refresh on the
    newer overlay."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-css",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        # Settle to PAINTED -> reconcile schedules the deferred consume. Do not
        # pump yet.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-B",
            ),
            source="test-build",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            ),
            source="test-pa",
        )
        assert machine.state is OverlayState.PAINTED
        # A focus-change supersede bumps the generation BEFORE the callback fires;
        # the reconcile drops the now-stale pending.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-sup",
        )
        assert machine.paint_generation == gen + 1
        assert controller._overlay_pending_postclick_refresh is None
        # Drive the NEW generation to PAINTED.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, snapshot_id="snap-C",
            ),
            source="test-build2",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen + 1, paint_state=PaintAckState.PAINTED,
            ),
            source="test-pa2",
        )
        # Now fire the stale call_soon callback (scheduled at gen) -> it must skip.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    # The stale callback did not drive a refresh on the gen+1 overlay.
    assert machine.state is OverlayState.PAINTED
    assert machine.paint_generation == gen + 1
    assert controller._overlay_pending_postclick_refresh is None


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.15: a focus refresh that lands between a
# badge click and its Input reply must not bypass the post-click clear.
#
# The interleave: the click is dispatched from PAINTED and captures that pair.
# One of the four FOCUS_CHANGE sources applies _refresh before the Input reply,
# which bumps the generation, so _feed_click_complete_refresh's gate 1 sees a
# mismatched pair. Before this bead the reply was dropped there, and a refresh
# that then FAILED put the PRE-CLICK badges back on screen over a screen the
# click had already changed.
#
# The two legs are told apart by snapshot identity, not by timing (boss ruling
# 2026-08-27, Option C): after the intervening refresh reaches PAINTED, the pin
# still holding the PRE-CLICK snapshot id means the refresh fell back, so the
# clear must fire; a different pin means a fresh list is on screen, so it must
# not.
# ---------------------------------------------------------------------------


def _drive_to_click_then_focus_refresh(machine, snapshot_id: str):
    """PAINTED at ``snapshot_id``, then a FOCUS_CHANGE bumps the generation.

    Returns the PRE-CLICK pair -- what the click carries as its
    ``overlay_dispatch_pair``. The machine is left in REFRESH_IN_FLIGHT one
    generation ahead of that pair, which is the exact interleave this bead is
    about.
    """

    sid, gen = _drive_to_painted(machine, snapshot_id)
    assert machine.pinned_snapshot_id == snapshot_id
    machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert machine.paint_generation == gen + 1
    return sid, gen


def test_focus_refresh_that_fails_between_click_and_reply_still_clears():
    """Failure leg. The intervening refresh fails, _refresh_fall_back restores
    the PRE-CLICK pin, and the click's clear must still happen -- the stale
    badges must not survive."""

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        sid, gen = _drive_to_click_then_focus_refresh(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-15f",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)

        # The intervening refresh FAILS -> _refresh_fall_back returns to PAINTED
        # at the bumped generation, still pinned to the PRE-CLICK snapshot.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, build_ok=False,
            ),
            source="test-build-fail",
        )
        assert machine.pinned_snapshot_id == "snap-A"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    # The clear fired: settle_after_click ON sends CLICK_COMPLETE from PAINTED
    # into POST_CLICK_SETTLING, whose entry dispatches the clear.
    assert machine.state is OverlayState.POST_CLICK_SETTLING


def test_focus_refresh_that_succeeds_between_click_and_reply_keeps_badges():
    """Success leg. The intervening refresh installs a genuinely new snapshot,
    so the click's clear must NOT fire -- clearing good current badges and
    entering the settling gap for nothing is strictly worse than doing
    nothing."""

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        sid, gen = _drive_to_click_then_focus_refresh(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-15s",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)

        # The intervening refresh SUCCEEDS and paints a new snapshot.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, snapshot_id="snap-B",
            ),
            source="test-build-ok",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen + 1, paint_state=PaintAckState.PAINTED,
            ),
            source="test-paint-ack",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    assert machine.state is OverlayState.PAINTED
    assert machine.pinned_snapshot_id == "snap-B"
    assert machine.paint_generation == gen + 1




def _dispatch_click_into_focus_refresh(
    machine, *, snapshot_id: str = "snap-A", trace_id: str = "tr-15",
):
    """Shared setup: PAINTED, a FOCUS_CHANGE bumps the generation, then the
    click ok reply lands against the now-stale pair.

    Returns ``(controller, sid, gen)`` where ``gen`` is the PRE-CLICK
    generation. The caller decides how the intervening refresh ends.
    """

    sid, gen = _drive_to_click_then_focus_refresh(machine, snapshot_id)
    controller = _controller(machine=machine)
    controller.loop = asyncio.get_running_loop()
    controller.background_tasks = []
    controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
    _wire_app(controller, lambda a, p: _click_response("ok"))
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
    return controller, sid, gen


def test_focus_refresh_that_times_out_between_click_and_reply_still_clears():
    """Failure leg, second variant. The refresh TIMEOUT reaches
    _refresh_fall_back by the same route a failed build response does, so the
    deferred clear must fire there too."""

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        controller, sid, gen = _dispatch_click_into_focus_refresh(
            machine, trace_id="tr-15t",
        )
        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-15t",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)

        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.TIMEOUT, overlay_session_id=sid,
                paint_generation=gen + 1,
            ),
            source="test-refresh-timeout",
        )
        assert machine.pinned_snapshot_id == "snap-A"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.POST_CLICK_SETTLING
    assert controller._overlay_pending_postclick_clear is None


def test_chained_same_session_supersedes_still_clear():
    """Why the pending clear is keyed on the SESSION, not on a generation.

    A second FOCUS_CHANGE supersedes the first refresh before it answers.
    _refresh_supersede restores the PRE-CLICK pin, so the stale badges are
    still the ones on screen and the clear is still owed. A generation key
    would have dropped the pending on this second bump and re-opened the
    hole."""

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        controller, sid, gen = _dispatch_click_into_focus_refresh(
            machine, trace_id="tr-15c",
        )
        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-15c",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_clear == (sid, 1, "snap-A")

        # A SECOND focus change supersedes the in-flight refresh.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-sup",
        )
        assert machine.paint_generation == gen + 2
        assert machine.state is OverlayState.REFRESH_IN_FLIGHT
        # The pending survives the supersede.
        assert controller._overlay_pending_postclick_clear == (sid, 1, "snap-A")

        # That second refresh fails too -> PAINTED at gen+2, pre-click pin.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 2, build_ok=False,
            ),
            source="test-build-fail-2",
        )
        assert machine.pinned_snapshot_id == "snap-A"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.POST_CLICK_SETTLING
    assert controller._overlay_pending_postclick_clear is None


def test_pending_clear_kept_through_paused_and_fires_on_resume():
    """The auto-hide fall-back leg lands in PAUSED, not PAINTED. The pending
    clear must be KEPT there -- a mic-resume restores the pinned snapshot, and
    that pin is the pre-click one, so the stale badges come back and the clear
    is still owed."""

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        controller, sid, gen = _dispatch_click_into_focus_refresh(
            machine, trace_id="tr-15p",
        )
        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-15p",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_clear == (sid, 1, "snap-A")

        # Mic pause during the refresh -> the auto-hide leg; the failed build
        # then resolves to PAUSED instead of PAINTED.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_PAUSE), source="test-pause",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, build_ok=False,
            ),
            source="test-build-fail-paused",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert machine.state is OverlayState.PAUSED
        # KEPT, not dropped.
        assert controller._overlay_pending_postclick_clear == (sid, 1, "snap-A")

        # The microphone resumes and the cached snapshot is still valid, so the
        # machine restores the PRE-CLICK badges to the screen.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True),
            source="test-resume",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.POST_CLICK_SETTLING
    assert controller._overlay_pending_postclick_clear is None


def test_pending_clear_dropped_when_a_new_session_replaces_the_overlay():
    """A new overlay session means the overlay the click was dispatched
    against is gone; the deferred clear must never fire on the replacement."""

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        controller, sid, gen = _dispatch_click_into_focus_refresh(
            machine, trace_id="tr-15n",
        )
        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-15n",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_clear == (sid, 1, "snap-A")

        # The user hides the overlay, then opens a fresh one.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.HIDE_NUMBERS), source="test-hide",
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_clear is None
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.SHOW_NUMBERS), source="test-show",
        )
        new_sid = machine.overlay_session_id
        new_gen = machine.paint_generation
        assert new_sid != sid
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=new_sid,
                paint_generation=new_gen, snapshot_id="snap-A",
            ),
            source="test-build-new",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=new_sid,
                paint_generation=new_gen, paint_state=PaintAckState.PAINTED,
            ),
            source="test-paint-new",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine

    controller, machine = asyncio.run(_run())
    # The new session is painted and untouched -- no clear fired on it, even
    # though it happens to be pinned to the SAME snapshot id.
    assert machine.state is OverlayState.PAINTED
    assert controller._overlay_pending_postclick_clear is None


def test_late_build_response_for_an_invalidated_generation_is_ignored():
    """Criterion 7. A build response for the superseded generation, arriving
    after the newer generation is already PAINTED, must not land on the newer
    state and must not leak a pin."""

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        controller, sid, gen = _dispatch_click_into_focus_refresh(
            machine, trace_id="tr-15l",
        )
        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-15l",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)

        # A second focus change invalidates the first refresh's build, then
        # that second refresh succeeds and paints.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-sup",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 2, snapshot_id="snap-B",
            ),
            source="test-build-2",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen + 2, paint_state=PaintAckState.PAINTED,
            ),
            source="test-paint-2",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert machine.state is OverlayState.PAINTED
        assert machine.pinned_snapshot_id == "snap-B"
        # NOW the invalidated build's late response arrives.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, snapshot_id="snap-late",
            ),
            source="test-build-late",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine

    controller, machine = asyncio.run(_run())
    # The late response did not move the machine, did not replace the pin, and
    # did not resurrect the deferred clear.
    assert machine.state is OverlayState.PAINTED
    assert machine.pinned_snapshot_id == "snap-B"
    # No pin leak: one snapshot pinned, no deferred prior left behind.
    assert machine.prior_pinned_snapshot_id is None
    assert machine.prior_pin_deferred is False
    assert controller._overlay_pending_postclick_clear is None


def test_settle_after_click_off_keeps_the_original_drop():
    """Criterion 8. With the flag OFF -- the shipped default -- nothing new
    fires: the reply is still dropped at gate 1, no pending clear is armed,
    and the machine is left exactly as it was before this bead."""

    async def _run():
        machine = ClickOverlayStateMachine()
        assert machine.settle_after_click is False
        controller, sid, gen = _dispatch_click_into_focus_refresh(
            machine, trace_id="tr-15o",
        )
        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-15o",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_clear is None

        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, build_ok=False,
            ),
            source="test-build-fail-off",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    assert machine.state is OverlayState.PAINTED
    assert machine.paint_generation == gen + 1
    assert machine.pinned_snapshot_id == "snap-A"
    assert controller._overlay_pending_postclick_clear is None


def test_all_four_focus_change_sources_share_the_single_apply_path():
    """Criterion 6, the documented argument, made executable.

    The deferred clear keys on the machine's session and pin, never on which
    hook produced the FOCUS_CHANGE. What has to be true for that to cover all
    four sources is that every one of them reaches the machine through the
    SAME ``_apply_overlay_event`` call with a FOCUS_CHANGE event. This drives
    each of the four and asserts exactly that, so a future source that bypasses
    the shared path fails here rather than silently escaping the repair.
    """

    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPEND,
    )

    def _fresh():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        _drive_to_painted(machine, "snap-A")
        controller = _controller(machine=machine)
        controller._apply_overlay_event = MagicMock()  # type: ignore[method-assign]
        controller._cancel_overlay_settle_refire = MagicMock()  # type: ignore[method-assign]
        controller._arm_overlay_settle_refire = MagicMock()  # type: ignore[method-assign]
        controller._overlay_focus_debouncer.should_fire.return_value = True
        return controller, machine

    applied: list[str] = []

    # 1. the foreground hook
    controller, machine = _fresh()
    controller._on_overlay_foreground_change(1234)
    applied.append("foreground")
    event = cast(MagicMock, controller._apply_overlay_event).call_args[0][0]
    assert event.kind is OverlayEventKind.FOCUS_CHANGE

    # 2. the menu pop-up hook
    controller, machine = _fresh()
    controller._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)
    applied.append("menu popup")
    event = cast(MagicMock, controller._apply_overlay_event).call_args[0][0]
    assert event.kind is OverlayEventKind.FOCUS_CHANGE

    # 3. the settle re-fire timer
    controller, machine = _fresh()
    controller._overlay_settle_handle = MagicMock()
    controller._on_overlay_settle_refire("foreground change")
    applied.append("settle refire")
    event = cast(MagicMock, controller._apply_overlay_event).call_args[0][0]
    assert event.kind is OverlayEventKind.FOCUS_CHANGE

    # 4. the browser proactive refresh on a keepalive tick
    controller, machine = _fresh()
    controller._overlay_browser_refresh_seconds = 1.0
    controller._overlay_browser_refresh_backoff = 1
    controller._overlay_browser_process_set = frozenset({"chrome.exe"})
    controller._overlay_tracked_identity = MagicMock()
    controller._overlay_tracked_identity.process_name = "chrome.exe"
    controller._overlay_last_paint_monotonic = 0.0
    controller._overlay_now_monotonic = lambda: 100.0  # type: ignore[method-assign]
    controller._overlay_in_proactive_apply = False
    controller._maybe_overlay_browser_refresh(machine)
    applied.append("browser refresh")
    event = cast(MagicMock, controller._apply_overlay_event).call_args[0][0]
    assert event.kind is OverlayEventKind.FOCUS_CHANGE

    assert applied == [
        "foreground", "menu popup", "settle refire", "browser refresh",
    ]

def test_refresh_that_already_finished_before_the_reply_still_clears():
    """The intervening refresh can FINISH before the click's reply arrives. No
    further apply follows, so nothing would run the post-apply reconcile; the
    arming path has to resolve the pending inline or the clear is lost."""

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        sid, gen = _drive_to_click_then_focus_refresh(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        # The refresh fails and settles back to PAINTED BEFORE the reply lands.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, build_ok=False,
            ),
            source="test-build-fail-early",
        )
        assert machine.state is OverlayState.PAINTED
        assert machine.pinned_snapshot_id == "snap-A"

        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-15e",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.POST_CLICK_SETTLING
    assert controller._overlay_pending_postclick_clear is None


def test_reply_for_a_replaced_overlay_session_arms_nothing():
    """The deferral is for a same-SESSION generation bump only. A reply whose
    overlay session has already been replaced keeps the original drop.

    The replacement is driven PAST the click's dispatch generation on purpose.
    A new session restarts paint_generation at 0
    (click_overlay_state.py:558), so a replacement that has painted once
    carries a generation the forward-bump test would reject on its own; only
    a replacement that has since refreshed reaches the same-session test. It
    is also pinned to the same snapshot id, which is what the reconcile would
    read as "the stale badges are still up" if the arming let it through.
    """

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        sid, gen = _drive_to_painted(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        # A whole new overlay session replaces the one the click resolved
        # against, and it lands on the same snapshot id.
        machine.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
        new_sid, new_gen = _drive_to_painted(machine, "snap-A")
        assert new_sid != sid
        # Carry the replacement PAST the click's generation, so the
        # forward-bump test cannot be what stops the arming.
        machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
        machine.apply(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=new_sid,
                paint_generation=new_gen + 1, build_ok=False,
            )
        )
        assert machine.state is OverlayState.PAINTED
        assert machine.paint_generation > gen
        assert machine.pinned_snapshot_id == "snap-A"

        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-15r",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, new_gen

    controller, machine, new_gen = asyncio.run(_run())
    assert controller._overlay_pending_postclick_clear is None
    assert machine.state is OverlayState.PAINTED
    assert machine.paint_generation == new_gen + 1


def test_deferred_clear_callback_re_checks_the_live_state_before_firing():
    """The consume is scheduled with call_soon, so an event can be processed
    between the schedule and the fire. A machine that has left PAINTED by then
    must not be cleared; the pending stays and the next reach-PAINTED decides.
    """

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        controller, sid, gen = _dispatch_click_into_focus_refresh(
            machine, trace_id="tr-15v",
        )
        await controller._send_snapshot_item_click(
            snapshot_id="snap-A", item_id="snap-A-item-2", trace_id="tr-15v",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        assert controller._overlay_pending_postclick_clear == (sid, 1, "snap-A")

        # Reach PAINTED holding the pre-click pin -> the consume is SCHEDULED.
        # Deliberately no pump here.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, build_ok=False,
            ),
            source="test-build-fail-sched",
        )
        assert machine.state is OverlayState.PAINTED
        # A focus change moves the machine off PAINTED before the callback runs.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-sup-race",
        )
        assert machine.state is OverlayState.REFRESH_IN_FLIGHT
        # NOW let the stale callback fire.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine, (sid, gen)

    controller, machine, (sid, gen) = asyncio.run(_run())
    # It did not clear an overlay that had left PAINTED, and it kept the
    # pending so the next reach-PAINTED still decides.
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert machine.paint_generation == gen + 2
    assert controller._overlay_pending_postclick_clear == (sid, 1, "snap-A")


def test_older_late_reply_does_not_overwrite_a_newer_pending_clear():
    """wh-overlay-slow-uia-stale-badges.23.1. The pending is ONE slot, and two
    clicks CAN have replies outstanding at once: ``_overlay_click_in_flight``
    is cleared in ``_send_snapshot_item_click``'s finally (main.py:3517),
    which runs on a TIMEOUT too, so a second click is allowed while the first
    click's late-response callback is still armed (app.py:150,
    ``_LATE_RESPONSE_GRACE_S = 15.0``). The late path feeds this same function
    with the FIRST click's dispatch pair and its dispatch-time snapshot id
    (main.py:383-386 for a late ok, 402-406 for a late re-walk refusal), so an
    OLDER defer can land after a newer one.

    It must not overwrite the newer one. Pins only advance on a successful
    paint, so the pin a later fall-back restores is the NEWER click's
    pre-click id; a slot holding the older id is then read as "a fresh
    snapshot is painted" and the clear the newer click is owed is discarded --
    the exact defect this bead closes.
    """

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        sid, gen = _drive_to_painted(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        # Click 1 is dispatched at (sid, gen) against pin snap-A and then times
        # out. A focus refresh SUCCEEDS and paints snap-B over it.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-23a-r1",
        )
        assert machine.state is OverlayState.REFRESH_IN_FLIGHT
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, snapshot_id="snap-B",
            ),
            source="test-23a-build-ok",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen + 1, paint_state=PaintAckState.PAINTED,
            ),
            source="test-23a-paint-ok",
        )
        assert machine.state is OverlayState.PAINTED
        assert machine.pinned_snapshot_id == "snap-B"

        # Click 2 is dispatched at (sid, gen + 1) against pin snap-B. A second
        # focus refresh bumps the machine again.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-23a-r2",
        )
        assert machine.paint_generation == gen + 2

        # Click 2's reply arrives first and arms the deferral.
        controller._feed_click_complete_refresh(
            overlay_dispatch_pair=(sid, gen + 1), trace_id="tr-23a-new",
            preclick_snapshot_id="snap-B", overlay_dispatch_ordinal=2,
        )
        assert controller._overlay_pending_postclick_clear is not None

        # Click 1's LATE reply arrives second, carrying the OLDER dispatch pair
        # and the OLDER pre-click id. This is the overwrite under test.
        controller._feed_click_complete_refresh(
            overlay_dispatch_pair=(sid, gen), trace_id="tr-23a-late",
            preclick_snapshot_id="snap-A", overlay_dispatch_ordinal=1,
        )

        # The second refresh FAILS -> _refresh_fall_back restores snap-B, the
        # pin click 2's stale badges are painted from.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 2, build_ok=False,
            ),
            source="test-23a-build-fail",
        )
        assert machine.pinned_snapshot_id == "snap-B"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine

    controller, machine = asyncio.run(_run())
    # Click 2's clear ran; the older late reply did not steal the slot.
    assert machine.state is OverlayState.POST_CLICK_SETTLING
    assert controller._overlay_pending_postclick_clear is None


def test_an_equal_generation_older_click_cannot_steal_the_pending_clear():
    """wh-overlay-slow-uia-stale-badges.23.3. ``paint_generation`` does not
    order two clicks. A badge click is deliberately routable while the machine
    is REFRESH_IN_FLIGHT (``route_click_n`` resolves it against the
    still-visible list when ``visible_window_is_foreground`` is not False), and
    ``_dispatch_snapshot_item_click`` captures the machine's CURRENT pair, so
    that click's dispatch generation is the refresh's. When that same refresh
    then SUCCEEDS it paints a new snapshot WITHOUT bumping the generation
    (``_bump_generation`` runs only on a refresh/restart/supersede dispatch),
    so the next click against the new list carries the SAME dispatch
    generation.

    Two outstanding clicks can therefore share a generation while owing clears
    against DIFFERENT pre-click snapshots. The overwrite rule must order them
    by dispatch, not by generation; a generation comparison is false on
    equality and lets the older click's late reply take the slot.
    """

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        sid, gen = _drive_to_painted(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        # A refresh bumps the machine to gen + 1. Click 1 is routed HERE,
        # against still-visible snap-A, so its dispatch pair is the machine's
        # current (sid, gen + 1). It times out and keeps its late callback.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-23c-r1",
        )
        assert machine.state is OverlayState.REFRESH_IN_FLIGHT
        assert machine.paint_generation == gen + 1

        # That refresh SUCCEEDS and paints snap-B -- at the SAME generation.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, snapshot_id="snap-B",
            ),
            source="test-23c-build-ok",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen + 1, paint_state=PaintAckState.PAINTED,
            ),
            source="test-23c-paint-ok",
        )
        assert machine.state is OverlayState.PAINTED
        assert machine.pinned_snapshot_id == "snap-B"
        assert machine.paint_generation == gen + 1

        # Click 2 is dispatched from PAINTED against snap-B, so it carries the
        # SAME dispatch generation as click 1. A second refresh bumps again.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-23c-r2",
        )
        assert machine.paint_generation == gen + 2

        # Click 2's reply arrives first and arms the deferral.
        controller._feed_click_complete_refresh(
            overlay_dispatch_pair=(sid, gen + 1), trace_id="tr-23c-new",
            preclick_snapshot_id="snap-B", overlay_dispatch_ordinal=2,
        )
        assert controller._overlay_pending_postclick_clear is not None

        # Click 1's LATE reply arrives second: same session, EQUAL dispatch
        # generation, older click, older pre-click id. The overwrite under
        # test.
        controller._feed_click_complete_refresh(
            overlay_dispatch_pair=(sid, gen + 1), trace_id="tr-23c-late",
            preclick_snapshot_id="snap-A", overlay_dispatch_ordinal=1,
        )

        # The second refresh FAILS -> _refresh_fall_back restores snap-B, the
        # pin click 2's stale badges are painted from.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 2, build_ok=False,
            ),
            source="test-23c-build-fail",
        )
        assert machine.pinned_snapshot_id == "snap-B"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine

    controller, machine = asyncio.run(_run())
    # Click 2's clear ran; the equal-generation older reply did not steal it.
    assert machine.state is OverlayState.POST_CLICK_SETTLING
    assert controller._overlay_pending_postclick_clear is None


def test_a_superseded_pending_refresh_still_clears_the_clicked_snapshot():
    """wh-overlay-slow-uia-stale-badges.23.4. A badge click routed while the
    machine is REFRESH_IN_FLIGHT carries the machine's CURRENT pair, so its
    reply MATCHES gate 1 and takes the refresh_in_flight branch -- the gate-1
    mismatch branch that arms the deferred clear never runs. That branch
    recorded only ``_overlay_pending_postclick_refresh``, a pair carrying no
    snapshot identity, so a same-session supersede dropped it with nothing left
    to say a clear was owed. The superseding refresh then FAILED,
    ``_refresh_fall_back`` restored the PRE-CLICK pin, and the badges for a
    click that had already changed the screen stayed on it -- the defect .15
    closed, reached down the other branch.
    """

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        sid, gen = _drive_to_painted(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        # A focus change starts refresh gen + 1. The click is routed HERE,
        # against still-visible snap-A, so its dispatch pair is the machine's
        # CURRENT pair and its reply matches gate 1.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-23d-r1",
        )
        assert machine.state is OverlayState.REFRESH_IN_FLIGHT
        assert machine.paint_generation == gen + 1

        controller._feed_click_complete_refresh(
            overlay_dispatch_pair=(sid, gen + 1), trace_id="tr-23d",
            preclick_snapshot_id="snap-A", overlay_dispatch_ordinal=1,
        )
        assert controller._overlay_pending_postclick_refresh == (sid, gen + 1)
        # The clear must be armed HERE, beside the pending refresh: it is the
        # only record carrying the pre-click snapshot id, and the pending
        # refresh does not survive the supersede below.
        assert controller._overlay_pending_postclick_clear == (
            sid, 1, "snap-A",
        )

        # A second focus change supersedes that refresh before it settles, so
        # the pending refresh is dropped on the pair mismatch.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-23d-r2",
        )
        assert machine.paint_generation == gen + 2
        assert controller._overlay_pending_postclick_refresh is None

        # That refresh FAILS -> _refresh_fall_back returns to PAINTED holding
        # the PRE-CLICK pin, so snap-A's badges are back over a screen the
        # click has already changed.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 2, build_ok=False,
            ),
            source="test-23d-build-fail",
        )
        assert machine.pinned_snapshot_id == "snap-A"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine

    controller, machine = asyncio.run(_run())
    # The deferred clear survived the supersede and ran on the fall-back.
    assert machine.state is OverlayState.POST_CLICK_SETTLING
    assert controller._overlay_pending_postclick_clear is None


def test_a_rolled_back_refresh_keeps_a_list_already_sent_to_the_display():
    """wh-overlay-slow-uia-stale-badges.23.6. The deferred clear treated
    ``pinned_snapshot_id == preclick_snapshot_id`` as proof that the pre-click
    badges are the ones on screen. That proof fails once a refresh paint has
    already been handed to the display process: ``_refresh_build_ok`` pins
    snap-B and emits its paint, the paint clears the .19 commit fence and goes
    on ``state_to_gui_queue``, and only THEN does the refresh deadline expire.
    ``_refresh_fall_back`` restores the snap-A pin and sends no clear, so
    Logic's pin says snap-A while the display already holds snap-B. The
    reconcile then replayed CLICK_COMPLETE and queued a clear behind snap-B,
    erasing a list Option C says must be RETAINED.
    """

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        sid, gen = _drive_to_painted(machine, "snap-A")
        cache = ClickSnapshotSummaryCache()
        cache.put("snap-A", _summary("snap-A", 1, 2))
        cache.put("snap-B", _summary("snap-B", 3, 4))
        controller = _controller(cache=cache, machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: {})

        # A focus change starts refresh gen + 1 while snap-A is still visible.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-23f-r1",
        )
        assert machine.state is OverlayState.REFRESH_IN_FLIGHT

        # The click was dispatched against snap-A at the PRE-refresh pair, so
        # its reply takes the gate-1 mismatch arm and arms the deferred clear.
        controller._feed_click_complete_refresh(
            overlay_dispatch_pair=(sid, gen), trace_id="tr-23f",
            preclick_snapshot_id="snap-A", overlay_dispatch_ordinal=1,
        )
        assert controller._overlay_pending_postclick_clear == (
            sid, 1, "snap-A",
        )

        # The refresh build SUCCEEDS with snap-B. The machine pins snap-B and
        # emits its paint; hand that paint to the REAL dispatch so it reaches
        # the display queue the way the running system delivers it.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, snapshot_id="snap-B",
            ),
            source="test-23f-build-ok",
        )
        assert machine.pinned_snapshot_id == "snap-B"
        paint = [
            effect
            for call in controller._perform_overlay_effects.call_args_list
            for effect in call.args[0]
            if effect.kind is EffectKind.DISPATCH_PAINT
        ][-1]
        await controller._dispatch_overlay_effects((paint,), trace_id="tr-23f")
        assert [
            item for item in _gui_items(controller)
            if item.get("action") == "paint_overlay"
        ], "the snap-B paint never reached the display queue"

        # The display process has not consumed snap-B yet when the refresh
        # deadline expires. The fall-back restores the snap-A pin and sends no
        # clear, so the pin now disagrees with what the display was given.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.TIMEOUT, overlay_session_id=sid,
                paint_generation=gen + 1,
            ),
            source="test-23f-timeout",
        )
        assert machine.state is OverlayState.PAINTED
        assert machine.pinned_snapshot_id == "snap-A"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.PAINTED, (
        "the deferred clear replayed CLICK_COMPLETE and erased snap-B, a list "
        "the display process had already been given"
    )
    # wh-overlay-slow-uia-stale-badges.23.8 changed this from "dropped" to
    # "kept dormant". The clear must not RUN here -- that is what the
    # assertion above proves -- but discarding the record loses the clear for
    # good, and a confirmed clear plus a resume can still put snap-A back on
    # screen after the click already succeeded. The record stays armed and
    # the pin test decides again later.
    assert controller._overlay_pending_postclick_clear == (
        machine.overlay_session_id, 1, "snap-A",
    )


def test_a_confirmed_clear_and_resume_still_owes_the_post_click_clear():
    """wh-overlay-slow-uia-stale-badges.23.8. The .23.6 fix retained the badges
    when the display had already been handed a different list, but it DROPPED
    the pending record to do it. The drop is permanent and the evidence is
    temporary: after the display confirms a clear, nothing is on screen, and a
    mic-resume re-paints the machine's pin -- snap-A, the list the click
    already consumed. The stale badges are back and no clear is owed any more.

    The evidence the decision needs already crosses the process boundary.
    ``OverlayStateChangedEvent`` carries the painted ``snapshot_id`` (the GUI
    sets it from the summary it painted, and only after every monitor
    succeeded), and the ``cleared`` acknowledgement is emitted only after
    ``_destroy_all`` succeeds. This test drives both through the real
    ``_handle_overlay_state_changed`` seam rather than a stub, so it also
    proves those fields arrive.
    """

    async def _run():
        machine = ClickOverlayStateMachine(settle_after_click=True)
        sid, gen = _drive_to_painted(machine, "snap-A")
        cache = ClickSnapshotSummaryCache()
        cache.put("snap-A", _summary("snap-A", 1, 2))
        cache.put("snap-B", _summary("snap-B", 3, 4))
        controller = _controller(cache=cache, machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: {})

        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        # Same opening as the .23.6 case: a focus refresh starts while snap-A
        # is on screen, and the click reply arrives on the mismatch arm.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.FOCUS_CHANGE), source="test-23g-r1",
        )
        assert machine.state is OverlayState.REFRESH_IN_FLIGHT
        controller._feed_click_complete_refresh(
            overlay_dispatch_pair=(sid, gen), trace_id="tr-23g",
            preclick_snapshot_id="snap-A", overlay_dispatch_ordinal=1,
        )
        assert controller._overlay_pending_postclick_clear == (
            sid, 1, "snap-A",
        )

        # The build succeeds with snap-B and its paint really reaches the
        # display queue.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, snapshot_id="snap-B",
            ),
            source="test-23g-build-ok",
        )
        paint = [
            effect
            for call in controller._perform_overlay_effects.call_args_list
            for effect in call.args[0]
            if effect.kind is EffectKind.DISPATCH_PAINT
        ][-1]
        await controller._dispatch_overlay_effects((paint,), trace_id="tr-23g")

        # The deadline expires and the fall-back restores the snap-A pin.
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.TIMEOUT, overlay_session_id=sid,
                paint_generation=gen + 1,
            ),
            source="test-23g-timeout",
        )
        assert machine.state is OverlayState.PAINTED
        assert machine.pinned_snapshot_id == "snap-A"
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # The display confirms it painted snap-B. Nothing to clear yet.
        await controller._handle_overlay_state_changed(
            OverlayStateChangedEvent(
                state="painted", overlay_session_id=sid,
                paint_generation=gen + 1, monitor_ids=(0,),
                snapshot_id="snap-B",
            ).to_dict()
        )
        await asyncio.sleep(0)

        # A mic-pause tears the overlay down and the display CONFIRMS it. The
        # screen is now empty, so no list can be erased any more.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_PAUSE), source="test-23g-pause",
        )
        assert machine.state is OverlayState.PAUSED
        await controller._handle_overlay_state_changed(
            OverlayStateChangedEvent(
                state="cleared", overlay_session_id=sid,
                paint_generation=gen + 1, monitor_ids=(),
                snapshot_id=None,
            ).to_dict()
        )
        await asyncio.sleep(0)

        # The resume re-paints the pin -- snap-A, the list the click already
        # consumed -- in the SAME overlay session, one generation on.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True),
            source="test-23g-resume",
        )
        assert machine.pinned_snapshot_id == "snap-A"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.POST_CLICK_SETTLING, (
        "snap-A is back on screen after its click already succeeded, and the "
        "owed post-click clear was discarded when snap-B was handed to the "
        "display, so nothing removes the stale badges"
    )
    assert controller._overlay_pending_postclick_clear is None


# ---------------------------------------------------------------------------
# Auto-open on ambiguous match (wh-n29v.111 / source leaf wh-ynr5zb).
#
# When click_element returns outcome="ambiguous" with ambiguous_item_ids, and
# the overlay is enabled AND overlay_auto_open_on_ambiguous is True AND the
# machine is in CLOSED, forward_click_element feeds an AUTO_OPEN OverlayEvent
# (carrying the suppressed notice + the reuse snapshot_id) into the state
# machine and SUPPRESSES today's immediate click notice. The auto-open build
# dispatches show_numbered_overlay restricted to the ambiguous finalists via
# item_id_filter (sourced from response.ambiguous_item_ids), and the Logic-side
# cached WalkSnapshotSummary is REPLACED with the filtered renumbered subset the
# Input handler returns. On any auto-open failure path (build-fail, paint-fail,
# walk timeout, paint timeout) the state machine fires the suppressed notice
# once (r2.9). When the overlay is disabled, auto-open is disabled, or the
# dispatch does not enter the machine, forward_click_element falls back to the
# plain notice path unchanged.
# ---------------------------------------------------------------------------


def _ambiguous_response(
    *, snapshot_id="snap-amb", item_ids=("snap-amb-item-1", "snap-amb-item-2"),
    matched_names=("Cancel", "Cancel all"), trace_id="tr-amb",
) -> dict:
    return ClickElementResponse(
        status="error",
        outcome="ambiguous",
        reason=None,
        matched_names=matched_names,
        snapshot_id=snapshot_id,
        snapshot_summary=_summary(snapshot_id, 1, 2, 3),
        matched_name=None,
        trace_id=trace_id,
        ambiguous_item_ids=item_ids,
    ).to_dict()


def _forward_controller(
    *, overlay_effective=True, auto_open=True, send_result=None,
    machine=None, cache=None,
):
    """A controller wired for forward_click_element auto-open tests.

    Reuses the integration ``_controller`` helper (so the overlay attributes are
    injected) and binds the few extra attributes ``forward_click_element``
    touches: the [click] config knobs, the first-use-hint no-op, and the fake
    app. ``send_result`` is the dict ``app.send_request`` resolves to.
    """

    controller = _controller(
        overlay_effective=overlay_effective, machine=machine, cache=cache,
    )
    # ``_controller`` set ``click_config`` to a MagicMock; pyright still sees the
    # declared ``ClickConfig`` (a frozen dataclass with read-only attributes)
    # once narrowing is lost across the helper boundary, so set the extra knobs
    # through an ``Any`` cast. At runtime these are plain MagicMock attribute
    # writes.
    cfg = cast(Any, controller.click_config)
    cfg.overlay_auto_open_on_ambiguous = auto_open
    cfg.enable_screen_reader_flag = False
    controller._click_disabled_notice_shown = False
    controller.background_tasks = []

    async def _no_hint(_trace_id):
        return None

    controller._maybe_show_first_use_hint = _no_hint  # type: ignore[method-assign]

    def _respond(action, params):
        # ``send_result`` is the click_element reply (the ambiguous response).
        # Every overlay build/pin action gets a VALID generation-aware reply so a
        # default-wired controller can drive the auto-open round trip without a
        # malformed show_numbered_overlay payload.
        if action == "click_element":
            return send_result
        if action == "show_numbered_overlay":
            sid = params.get("overlay_session_id", 0)
            gen = params.get("paint_generation", 0)
            snap = params.get("snapshot_id", "snap-amb") or "snap-amb"
            return _show_response(snap, sid, gen, 1, 2)
        return _pin_response(action, params)

    _wire_app(controller, _respond)
    return controller


def _amb_query():
    from services.wheelhouse.ui.element_types import ElementQuery

    return ElementQuery("cancel", "Button", None, None, "cancel")


def test_ambiguous_auto_open_applies_event_and_suppresses_notice():
    """Outcome=ambiguous with overlay+auto-open on and CLOSED machine: an
    AUTO_OPEN OverlayEvent is applied (notice stashed, reuse snapshot carried)
    and the immediate notice path is NOT taken."""

    async def _run():
        controller = _forward_controller(send_result=_ambiguous_response())
        controller.loop = asyncio.get_running_loop()
        machine = controller.click_overlay_state
        # No immediate notice: the standalone notice path must be suppressed.
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        await controller.forward_click_element(_amb_query(), "tr-amb")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    # The machine left CLOSED via AUTO_OPEN and the suppressed notice is held in
    # pending_ambiguous_notice (until painted clears it or a failure fires it).
    assert machine.state is not OverlayState.CLOSED
    assert machine.pending_ambiguous_notice is not None
    assert machine.pending_ambiguous_notice.outcome == "ambiguous"
    assert machine.pending_ambiguous_notice.matched_names == (
        "Cancel", "Cancel all",
    )
    # The immediate notice path was suppressed (no show_click_notice forwarded).
    cast(MagicMock, controller._forward_click_notice).assert_not_called()
    # show_numbered_overlay was dispatched (auto-open build), NOT a notice.
    sent_actions = [a for a, _ in controller._sent]  # type: ignore[attr-defined]
    assert "show_numbered_overlay" in sent_actions


def test_ambiguous_auto_open_dispatches_filter_snapshot_and_generation():
    """The auto-open build dispatches show_numbered_overlay with item_id_filter
    == list(response.ambiguous_item_ids), snapshot_id == the response snapshot,
    and the machine's (overlay_session_id, paint_generation)."""

    async def _run():
        item_ids = ("snap-amb-item-1", "snap-amb-item-2")
        amb = _ambiguous_response(item_ids=item_ids)

        def _respond(action, params):
            if action == "click_element":
                return amb
            if action == "show_numbered_overlay":
                sid = params["overlay_session_id"]
                gen = params["paint_generation"]
                # The handler filters + renumbers to 1..K; echo a 2-item summary.
                return _show_response("snap-amb", sid, gen, 1, 2)
            return _pin_response(action, params)

        controller = _forward_controller(send_result=amb)
        controller.loop = asyncio.get_running_loop()
        # Re-wire the app to a generation-aware responder.
        _wire_app(controller, _respond)
        machine = controller.click_overlay_state
        await controller.forward_click_element(_amb_query(), "tr-amb")
        await _settle(controller)
        return controller, machine, item_ids

    controller, machine, item_ids = asyncio.run(_run())
    show = [
        (a, p) for a, p in controller._sent  # type: ignore[attr-defined]
        if a == "show_numbered_overlay"
    ]
    assert len(show) == 1
    params = show[0][1]
    assert params["snapshot_id"] == "snap-amb"
    assert params["item_id_filter"] == list(item_ids)
    assert params["overlay_session_id"] == machine.overlay_session_id


def test_ambiguous_auto_open_replaces_cache_with_filtered_subset():
    """On the auto-open build-response, the Logic cache is REPLACED with the
    filtered renumbered subset the Input handler returns; the full unfiltered
    summary is no longer the cached value for that snapshot id."""

    async def _run():
        amb = _ambiguous_response()

        def _respond(action, params):
            if action == "click_element":
                return amb
            if action == "show_numbered_overlay":
                sid = params["overlay_session_id"]
                gen = params["paint_generation"]
                # The Input handler renumbers the kept set to 1..2 (two finalists).
                return _show_response("snap-amb", sid, gen, 1, 2)
            return _pin_response(action, params)

        controller = _forward_controller(send_result=amb)
        controller.loop = asyncio.get_running_loop()
        _wire_app(controller, _respond)
        await controller.forward_click_element(_amb_query(), "tr-amb")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    cached = controller.click_snapshot_summary_cache.resolve("snap-amb").summary
    assert cached is not None
    # The filtered subset has exactly the two renumbered finalists, NOT the full
    # three-item walk the original click_element response carried.
    assert [i.display_number for i in cached.items] == [1, 2]


@pytest.mark.parametrize("overlay_effective,auto_open", [(False, True), (True, False)])
def test_ambiguous_falls_back_to_notice_when_disabled(overlay_effective, auto_open):
    """overlay disabled OR auto-open disabled: the ambiguous outcome takes the
    plain notice path -- no auto-open, no suppression."""

    async def _run():
        controller = _forward_controller(
            overlay_effective=overlay_effective, auto_open=auto_open,
            send_result=_ambiguous_response(),
        )
        controller.loop = asyncio.get_running_loop()
        machine = controller.click_overlay_state
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        await controller.forward_click_element(_amb_query(), "tr-amb")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    # No auto-open: the machine stays CLOSED and no overlay build was dispatched.
    assert machine.state is OverlayState.CLOSED
    sent_actions = [a for a, _ in controller._sent]  # type: ignore[attr-defined]
    assert "show_numbered_overlay" not in sent_actions
    # The plain ambiguous notice was forwarded.
    notice = cast(MagicMock, controller._forward_click_notice)
    notice.assert_called_once()
    _, kwargs = notice.call_args
    assert kwargs.get("outcome") == "ambiguous"


def test_ambiguous_falls_back_to_notice_when_no_item_ids():
    """An ambiguous response missing ambiguous_item_ids cannot drive a restricted
    auto-open, so it falls back to the plain notice path."""

    async def _run():
        controller = _forward_controller(
            send_result=_ambiguous_response(item_ids=None),
        )
        controller.loop = asyncio.get_running_loop()
        machine = controller.click_overlay_state
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        await controller.forward_click_element(_amb_query(), "tr-amb")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    cast(MagicMock, controller._forward_click_notice).assert_called_once()


def _auto_open_machine_in_flight(notice, *, snapshot_id="snap-amb"):
    """Return (machine, sid, gen) after applying AUTO_OPEN (walk_in_flight)."""
    machine = ClickOverlayStateMachine()
    machine.apply(
        OverlayEvent(
            OverlayEventKind.AUTO_OPEN, notice=notice, snapshot_id=snapshot_id,
        )
    )
    return machine, machine.overlay_session_id, machine.paint_generation


def _suppressed_notice():
    from services.wheelhouse.shared.click_notice import ClickNoticeEvent

    return ClickNoticeEvent(
        outcome="ambiguous", reason=None, matched_name=None,
        matched_names=("Cancel", "Cancel all"), spoken_name="cancel",
        app_friendly_name="", snapshot_id="snap-amb", trace_id="tr-amb",
    )


@pytest.mark.parametrize(
    "failure",
    ["build_fail", "paint_fail", "walk_timeout", "paint_timeout"],
)
def test_auto_open_failure_paths_fire_suppressed_notice_once(failure):
    """r2.9: on each auto-open failure path the suppressed notice fires exactly
    once and the machine recovers to CLOSED."""

    notice = _suppressed_notice()

    async def _run():
        machine, sid, gen = _auto_open_machine_in_flight(notice)
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, lambda a, p: {})
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        if failure in ("build_fail", "walk_timeout"):
            # walk_in_flight: a failed build OR a walk timeout fires the pending
            # notice on error -> closed.
            kind = (
                OverlayEventKind.BUILD_RESPONSE
                if failure == "build_fail"
                else OverlayEventKind.TIMEOUT
            )
            controller._apply_overlay_event(
                OverlayEvent(
                    kind=kind, overlay_session_id=sid, paint_generation=gen,
                    build_ok=False,
                ),
                source=f"test-{failure}",
            )
        else:
            # Advance to paint_in_flight via a good build-response, then fail the
            # paint (paint_fail) or time it out (paint_timeout).
            controller._apply_overlay_event(
                OverlayEvent(
                    OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                    paint_generation=gen, snapshot_id="snap-amb",
                ),
                source="test-build-ok",
            )
            assert machine.state is OverlayState.PAINT_IN_FLIGHT
            if failure == "paint_fail":
                controller._apply_overlay_event(
                    OverlayEvent(
                        OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                        paint_generation=gen,
                        paint_state=PaintAckState.FAILED,
                    ),
                    source="test-paint-fail",
                )
            else:
                controller._apply_overlay_event(
                    OverlayEvent(
                        OverlayEventKind.TIMEOUT, overlay_session_id=sid,
                        paint_generation=gen,
                    ),
                    source="test-paint-timeout",
                )
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    # The machine recovered to CLOSED and the suppressed notice fired EXACTLY
    # once with the original ambiguous payload.
    assert machine.state is OverlayState.CLOSED
    notice_mock = cast(MagicMock, controller._forward_click_notice)
    notice_mock.assert_called_once()
    _, kwargs = notice_mock.call_args
    assert kwargs.get("outcome") == "ambiguous"
    assert kwargs.get("matched_names") == ("Cancel", "Cancel all")


def test_auto_open_success_path_does_not_fire_suppressed_notice():
    """On the success path (build-ok -> paint-ack painted) the suppressed notice
    is NOT fired; it is cleared by entry through painted."""

    notice = _suppressed_notice()

    async def _run():
        machine, sid, gen = _auto_open_machine_in_flight(notice)
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, lambda a, p: _pin_response(a, p))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
            return_value=None
        )

        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-amb",
            ),
            source="test-build-ok",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            ),
            source="test-paint-ok",
        )
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.PAINTED
    cast(MagicMock, controller._forward_click_notice).assert_not_called()


def test_auto_open_apply_exception_falls_back_to_notice_and_unwedges_machine():
    """wh-n29v.112.1: an exception from the AUTO_OPEN apply/stash region -- after
    the machine has already committed walk_in_flight -- must NOT propagate. It
    must fall back to the plain notice AND reset the machine to CLOSED, honoring
    the _perform_auto_open_ambiguous "Never raises -> plain notice still fires"
    contract. Without the guard the exception escapes forward_click_element
    (silent loss of user feedback) and leaves the overlay machine wedged in
    walk_in_flight, so every later ambiguous click fails the CLOSED gate and the
    overlay feature is dead until process restart."""

    async def _run():
        controller = _forward_controller(send_result=_ambiguous_response())
        controller.loop = asyncio.get_running_loop()
        machine = controller.click_overlay_state
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        # Reproduce the finding: apply commits the transition (closed ->
        # walk_in_flight) and then an unguarded later step raises. machine.apply
        # is the pure transition; raising right after it models a reconcile
        # helper in _apply_overlay_event (or the stash write) failing while the
        # machine is already walk_in_flight.
        def _apply_then_raise(event, *, source):
            machine.apply(event)
            raise RuntimeError("injected auto-open apply failure")

        controller._apply_overlay_event = _apply_then_raise  # type: ignore[method-assign]

        # Must NOT raise out of forward_click_element.
        await controller.forward_click_element(_amb_query(), "tr-amb")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    # The plain notice fired exactly once: user feedback was not lost.
    notice = cast(MagicMock, controller._forward_click_notice)
    notice.assert_called_once()
    _, kwargs = notice.call_args
    assert kwargs.get("outcome") == "ambiguous"
    # The machine was un-wedged back to CLOSED (not left in walk_in_flight).
    assert machine.state is OverlayState.CLOSED
    # The stash was cleared so no stale filter leaks into a later session.
    assert controller._overlay_auto_open_filter is None


def test_hide_command_clears_auto_open_filter_stash():
    """wh-n29v.114.1: hide_numbers drives the machine to CLOSED via
    handle_overlay_command, which applies HIDE_NUMBERS directly and bypasses
    _apply_overlay_event's closed-entry cleanup. An outstanding auto-open
    item_id_filter stash must still be cleared on that path, so a stale filter
    cannot leak into a later overlay session (the design contract is "every path
    that abandons an auto-open clears the slot")."""

    async def _run():
        # Drive a real machine to PAINTED so 'hide' transitions it to CLOSED.
        notice = _suppressed_notice()
        machine, sid, gen = _auto_open_machine_in_flight(notice)
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, lambda a, p: _pin_response(a, p))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
            return_value=None
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-amb",
            ),
            source="test-build-ok",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            ),
            source="test-paint-ok",
        )
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        assert machine.state is OverlayState.PAINTED
        # Simulate an auto-open filter still stashed (e.g. a build that never
        # consumed it). The SHOW/build path above does not touch the stash;
        # _take_auto_open_filter runs only for an AUTO_OPEN dispatch build.
        controller._overlay_auto_open_filter = ((sid, gen), ["snap-amb-item-1"])

        # 'hide numbers' tears the session down through handle_overlay_command,
        # which bypasses _apply_overlay_event's closed-entry stash cleanup.
        await controller.handle_overlay_command("hide", "tr-hide")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    # The stash was cleared on the hide -> closed path.
    assert controller._overlay_auto_open_filter is None


def test_hide_command_cancels_pending_settle_refire():
    """Codex finding wh-overlay-nested-dupes.1.4: hide_numbers drives the machine
    to CLOSED via handle_overlay_command, which applies HIDE_NUMBERS directly and
    bypasses _apply_overlay_event's closed-entry settle cancel. A pending settle
    re-fire armed by a coalesced foreground/menu event during the session must be
    cancelled on that path too -- otherwise an immediate 'show numbers' lets the
    stale timer fire a FOCUS_CHANGE into the fresh session's build and restart
    the user-requested walk (an unnecessary walk + generation bump)."""

    async def _run():
        notice = _suppressed_notice()
        machine, sid, gen = _auto_open_machine_in_flight(notice)
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, lambda a, p: _pin_response(a, p))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
            return_value=None
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-amb",
            ),
            source="test-build-ok",
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            ),
            source="test-paint-ok",
        )
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        assert machine.state is OverlayState.PAINTED
        # A coalesced foreground/menu event armed the settle timer earlier in
        # the painted session; model the armed timer with a fake handle.
        pending = MagicMock()
        controller._overlay_settle_handle = pending

        # 'hide numbers' tears the session down through handle_overlay_command,
        # which bypasses _apply_overlay_event's closed-entry settle cancel.
        await controller.handle_overlay_command("hide", "tr-hide")
        await _settle(controller)
        return controller, machine, pending

    controller, machine, pending = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    # The pending settle was cancelled on the hide -> closed path.
    pending.cancel.assert_called_once()
    assert controller._overlay_settle_handle is None


# ---------------------------------------------------------------------------
# wh-n29v.117: the floating-button "walking" progress cue emit.
#
# ``_overlay_dispatch_build`` is the single funnel every walk-start passes
# through. It must enqueue a plain-dict ``overlay_walk_cue`` action onto
# state_to_gui_queue: active:True at walk-start (before the send_request) and
# active:False at BOTH the build-success feed path AND the build-failure /
# timeout feed path. This rides the existing GUI state queue (no new shared/
# schema, no new EffectKind) and the GUI consumer is defensive.
# ---------------------------------------------------------------------------


def _walk_cue_items(controller) -> list[dict]:
    return [
        m for m in _gui_items(controller)
        if m.get("action") == "overlay_walk_cue"
    ]


def test_overlay_walk_cue_emitted_active_true_at_walk_start():
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(
            controller, lambda a, p: _walk_response("snap-w", sid, gen, 1, 2)
        )
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SHOW_NUMBERS,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    cues = _walk_cue_items(controller)
    assert cues, "no overlay_walk_cue action was enqueued"
    # The first cue is the walk-start active:True.
    assert cues[0]["active"] is True
    # wh-n29v.118 / wh-n29v.120.1, retargeted by
    # wh-overlay-slow-uia-stale-badges.3: the walk-start payload carries the
    # build's own awaited window PLUS response_timeout_ms, so the GUI fallback
    # outlasts the full success-path latency (the walk send_request, bounded
    # here by screen_read_timeout_ms, then the PIN_SNAPSHOT ack await, bounded
    # by response_timeout_ms). A fallback sized for the walk alone would clear
    # the cue during a slow or hung pin before the numbers paint.
    # wh-overlay-slow-uia-stale-badges.3.1.1 adds the sentence-end wait bound:
    # the cue is emitted BEFORE _wait_for_open_sentence_end, so a walk that
    # holds for the open dictation sentence spends up to that bound before the
    # send even starts.
    assert (
        cues[0]["walk_timeout_ms"]
        == controller.click_config.screen_read_timeout_ms
        + controller.click_config.response_timeout_ms
        + int(_OVERLAY_READ_SENTENCE_WAIT_MAX_S * 1000)
    )


def test_overlay_walk_cue_walk_timeout_covers_walk_plus_pin():
    """wh-n29v.120.1 regression: on the success path the cue is cleared by the
    GUI when paint_overlay arrives, and paint_overlay is enqueued only after the
    PIN_SNAPSHOT ack await (bounded by response_timeout_ms) that runs after the
    walk send_request (bounded, for a screen read, by screen_read_timeout_ms).
    The walk-start cue must therefore carry a GUI fallback bound of the walk
    window plus the pin window, so the fallback never fires before
    paint_overlay on a slow or hung pin.

    wh-overlay-slow-uia-stale-badges.3.1.1 adds a third term: the cue is
    emitted before ``_wait_for_open_sentence_end``, which holds a walk
    requested mid-sentence for up to ``_OVERLAY_READ_SENTENCE_WAIT_MAX_S``
    before the send starts, so the bound covers wait + walk + pin.
    """
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(
            controller, lambda a, p: _walk_response("snap-w", sid, gen, 1, 2)
        )
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SHOW_NUMBERS,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    cues = _walk_cue_items(controller)
    assert cues, "no overlay_walk_cue action was enqueued"
    rt = controller.click_config.response_timeout_ms
    read = controller.click_config.screen_read_timeout_ms
    wait_ms = int(_OVERLAY_READ_SENTENCE_WAIT_MAX_S * 1000)
    assert cues[0]["walk_timeout_ms"] == read + rt + wait_ms
    # The carried bound must strictly exceed the single-walk bound that an
    # earlier slice used, which is the gap deepseek caught.
    assert cues[0]["walk_timeout_ms"] > read


def test_overlay_walk_cue_auto_open_bound_carries_no_sentence_wait():
    """wh-overlay-slow-uia-stale-badges.3.1.1: an AUTO_OPEN build never waits.

    ``_wait_for_open_sentence_end`` runs only when the build sends
    ``start_overlay_walk`` (``walks_input``). AUTO_OPEN sends
    ``show_numbered_overlay``, a lookup in the snapshot store Input already
    holds, so its cue bound stays the lookup window plus the pin-ack window --
    both ``response_timeout_ms``. Adding the sentence-wait term here would
    leave the dot on five seconds past anything this build can take.
    """
    machine, sid, gen = _auto_open_machine_in_flight(
        _suppressed_notice(), snapshot_id="snap-a"
    )

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _record_build_timeouts(controller, sid, gen)
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.AUTO_OPEN,
            snapshot_id="snap-a",
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    cues = _walk_cue_items(controller)
    assert cues, "no overlay_walk_cue action was enqueued"
    rt = controller.click_config.response_timeout_ms
    assert cues[0]["walk_timeout_ms"] == rt + rt


def test_overlay_walk_cue_not_logic_cleared_on_build_success():
    """wh-n29v.119.2: on a SUCCESSFUL build Logic must NOT enqueue the
    terminating active:False in ``_feed``. The success transition schedules
    PIN_SNAPSHOT then DISPATCH_PAINT, and ``_overlay_send_pin`` awaits the pin
    ack (up to response_timeout_ms) before ``paint_overlay`` is enqueued.
    Clearing the cue in ``_feed`` would drop the dot during that window, so the
    user would see neither the cue nor the numbers -- defeating the
    latency-budget affordance. The GUI clears the cue as a backstop when
    ``paint_overlay`` arrives; the fallback timer covers a success-without-paint
    (summary cache miss).
    """
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        def _respond(action, params):
            if action in ("pin_snapshot", "unpin_snapshot"):
                return _pin_response(action, params)
            return _walk_response("snap-w", sid, gen, 1, 2)

        _wire_app(controller, _respond)
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SHOW_NUMBERS,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    cues = _walk_cue_items(controller)
    assert cues, "no overlay_walk_cue action was enqueued"
    assert cues[0]["active"] is True
    # Logic does NOT clear the cue on the success path.
    assert all(c["active"] is True for c in cues)
    # A paint_overlay follows; the GUI uses it as the clear backstop.
    actions = [m.get("action") for m in _gui_items(controller)]
    assert "paint_overlay" in actions
    # The walking cue precedes the paint in queue order: the dot stays on while
    # the numbers are prepared and is never cleared before paint_overlay.
    assert actions.index("overlay_walk_cue") < actions.index("paint_overlay")


def test_overlay_walk_cue_not_cleared_before_paint_with_slow_pin():
    """wh-n29v.119.2 regression: even when the pin ack is SLOW, the cue must
    stay active until ``paint_overlay`` is enqueued. The success transition
    awaits PIN_SNAPSHOT before DISPATCH_PAINT, so a slow/hung input process
    delays the paint; clearing the cue at build success would make the dot
    vanish during that delay while no numbers are on screen yet.
    """
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
            params = dict(params or {})
            if action in ("pin_snapshot", "unpin_snapshot"):
                # Simulate a slow input process: yield several times before the
                # pin ack returns, delaying the subsequent paint dispatch.
                for _ in range(5):
                    await asyncio.sleep(0)
                return _pin_response(action, params)
            return _walk_response("snap-w", sid, gen, 1, 2)

        controller.app = MagicMock()
        controller.app.send_request = _send_request
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SHOW_NUMBERS,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    cues = _walk_cue_items(controller)
    # No active:False cue from Logic at all on the success path.
    assert all(c["active"] is True for c in cues)
    # The paint is still enqueued (the GUI clears the cue on receipt).
    actions = [m.get("action") for m in _gui_items(controller)]
    assert "paint_overlay" in actions


def test_overlay_walk_cue_cleared_on_build_failure():
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        # A malformed reply is treated as a build failure: the failure feed
        # path must also enqueue the terminating active:False cue.
        _wire_app(controller, lambda a, p: {"garbage": True})
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SHOW_NUMBERS,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    cues = _walk_cue_items(controller)
    assert cues[0]["active"] is True
    assert cues[-1]["active"] is False


def test_overlay_walk_cue_cleared_on_build_timeout():
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
            raise asyncio.TimeoutError()

        controller.app = MagicMock()
        controller.app.send_request = _send_request
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SHOW_NUMBERS,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    cues = _walk_cue_items(controller)
    # walk-start active:True even though the request times out, then the
    # timeout feed path enqueues the terminating active:False.
    assert cues[0]["active"] is True
    assert cues[-1]["active"] is False


@pytest.mark.parametrize(
    "send_exc", [asyncio.TimeoutError(), RuntimeError("boom")],
    ids=["timeout", "send-failure"],
)
def test_build_send_fault_companion_log_is_below_error(send_exc, caplog):
    """One send_request fault must produce ONE ERROR record, not two.

    WheelHouseApp.send_request already logs every timeout / send failure at
    ERROR before re-raising, and the error-notification handler turns EVERY
    ERROR record into its own Windows notification popup. A single slow
    start_overlay_walk therefore popped TWO notifications (observed live
    2026-08-08 17:57; wh-duplicate-error-popup). The build awaiter's
    companion line keeps the overlay context in the log -- at WARNING.
    """
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
            raise send_exc

        controller.app = MagicMock()
        controller.app.send_request = _send_request
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SHOW_NUMBERS,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr")
        await _settle(controller)
        return controller

    with caplog.at_level(logging.DEBUG):
        asyncio.run(_run())
    companions = [
        r for r in caplog.records
        if "no reply within" in r.getMessage()
        or "send_request failed" in r.getMessage()
    ]
    assert companions, "expected the build awaiter's companion log line"
    assert all(r.levelno < logging.ERROR for r in companions)


# ---------------------------------------------------------------------------
# wh-n29v.121: proactive refresh while PAINTED over a browser window. Cached
# badge positions go stale fast on dynamic Chromium pages; the keepalive tick
# feeds one FOCUS_CHANGE (the same event the focus/menu hooks reuse, mapping
# to a REFRESH in PAINTED) once the overlay has been painted longer than
# overlay_browser_refresh_seconds AND the tracked window is a browser/Electron
# process. Non-browser windows, sub-window ages, a zero window (opt-out), a
# missing tracked identity, and any non-PAINTED state must all skip.
# ---------------------------------------------------------------------------


async def _drive_browser_refresh_case(
    *,
    process_name: str = "brave.exe",
    window_s: float = 10.0,
    advance_s: float = 15.0,
    identity_none: bool = False,
    paint_clock: float = 3.0,
):
    """Drive show -> painted, set the tracked identity, tick the keepalive.

    Returns (controller, machine, sent) where ``sent`` holds only the sends
    made by the keepalive tick (the show/paint sends are cleared first).
    """
    from services.wheelhouse.shared.overlay_state_changed import (
        OverlayStateChangedEvent,
    )
    from services.wheelhouse.overlay_focus_hooks import ForegroundIdentity

    clock = {"t": 0.0}
    controller = _controller()
    controller.loop = asyncio.get_running_loop()
    controller.background_tasks = []
    controller._overlay_browser_refresh_seconds = float(window_s)
    controller._overlay_browser_process_set = frozenset(
        {"brave.exe", "chrome.exe"}
    )
    controller._overlay_now_monotonic = lambda: clock["t"]
    machine = controller.click_overlay_state

    def _respond(action, params):
        sid = params.get("overlay_session_id", 0)
        gen = params.get("paint_generation", 0)
        if action == "start_overlay_walk":
            return _walk_response("snap-br", sid, gen, 1, 2)
        return _pin_response(action, params)

    sent = _wire_app(controller, _respond)
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value=None
    )
    await controller.handle_overlay_command("show", "tr-br")
    await _settle(controller)
    sid, gen = machine.overlay_session_id, machine.paint_generation
    clock["t"] = paint_clock
    ack = OverlayStateChangedEvent(
        state="painted", overlay_session_id=sid, paint_generation=gen,
        monitor_ids=(0,), snapshot_id="snap-br",
    ).to_dict()
    await controller._handle_overlay_state_changed(ack)
    await _settle(controller)
    assert machine.state is OverlayState.PAINTED
    if identity_none:
        controller._overlay_tracked_identity = None
    else:
        controller._overlay_tracked_identity = ForegroundIdentity(
            hwnd=11, pid=22, process_name=process_name,
            window_creation_time=1,
        )
    controller._overlay_cancel_keepalive_timer()
    sent.clear()
    clock["t"] = paint_clock + advance_s
    controller._fire_overlay_keepalive()
    await _settle(controller)
    controller._overlay_cancel_keepalive_timer()
    return controller, machine, sent


def _refresh_walks(sent) -> list:
    return [a for a, _ in sent if a == "start_overlay_walk"]


def test_browser_refresh_fires_past_window_over_browser_window():
    async def _run():
        return await _drive_browser_refresh_case(
            process_name="brave.exe", window_s=10.0, advance_s=15.0,
        )

    controller, machine, sent = asyncio.run(_run())
    # The PAINTED-entry stamp was taken at the ack (clock 3.0), so the age at
    # the tick is 15.0 >= 10.0 and one refresh walk was dispatched.
    assert controller._overlay_last_paint_monotonic == 3.0
    assert len(_refresh_walks(sent)) == 1
    assert machine.state is not OverlayState.PAINTED


def test_browser_refresh_process_name_matching_is_case_folded():
    async def _run():
        return await _drive_browser_refresh_case(
            process_name="Brave.EXE", window_s=10.0, advance_s=15.0,
        )

    _controller_, machine, sent = asyncio.run(_run())
    assert len(_refresh_walks(sent)) == 1


def test_browser_refresh_skips_non_browser_process():
    async def _run():
        return await _drive_browser_refresh_case(
            process_name="notepad.exe", window_s=10.0, advance_s=60.0,
        )

    _controller_, machine, sent = asyncio.run(_run())
    assert _refresh_walks(sent) == []
    assert machine.state is OverlayState.PAINTED


def test_browser_refresh_skips_below_window():
    async def _run():
        return await _drive_browser_refresh_case(
            process_name="brave.exe", window_s=10.0, advance_s=5.0,
        )

    _controller_, machine, sent = asyncio.run(_run())
    assert _refresh_walks(sent) == []
    assert machine.state is OverlayState.PAINTED


def test_browser_refresh_disabled_when_window_zero():
    async def _run():
        return await _drive_browser_refresh_case(
            process_name="brave.exe", window_s=0.0, advance_s=3600.0,
        )

    _controller_, machine, sent = asyncio.run(_run())
    assert _refresh_walks(sent) == []
    assert machine.state is OverlayState.PAINTED


def test_browser_refresh_skips_without_tracked_identity():
    async def _run():
        return await _drive_browser_refresh_case(
            identity_none=True, window_s=10.0, advance_s=60.0,
        )

    _controller_, machine, sent = asyncio.run(_run())
    assert _refresh_walks(sent) == []
    assert machine.state is OverlayState.PAINTED


def test_browser_refresh_only_fires_in_painted_state():
    """PAUSED gets the TTL re-put but never a proactive refresh (a paused
    overlay is invisible; refreshing it would walk a window the user cannot
    see), and any other non-PAINTED state is skipped too."""
    from types import SimpleNamespace
    from services.wheelhouse.overlay_focus_hooks import ForegroundIdentity

    controller = _controller()
    controller._overlay_browser_refresh_seconds = 10.0
    controller._overlay_browser_process_set = frozenset({"brave.exe"})
    controller._overlay_now_monotonic = lambda: 100.0
    controller._overlay_last_paint_monotonic = 0.0
    controller._overlay_tracked_identity = ForegroundIdentity(
        hwnd=11, pid=22, process_name="brave.exe", window_creation_time=1,
    )
    controller._apply_overlay_event = MagicMock()  # type: ignore[method-assign]
    for state in (
        OverlayState.PAUSED,
        OverlayState.CLOSED,
        OverlayState.REFRESH_IN_FLIGHT,
        OverlayState.WALK_IN_FLIGHT,
    ):
        controller._maybe_overlay_browser_refresh(SimpleNamespace(state=state))
    controller._apply_overlay_event.assert_not_called()


def test_keepalive_first_tick_fires_immediately_on_arm():
    """wh-overlay-snapshot-keepalive residual edge: when a FAILED refresh
    restores the prior snapshot, that snapshot's ttl_anchor was last slid up
    to ~one keepalive interval before the refresh began; re-arming with a
    full fresh interval could put the next slide past the TTL, where
    refresh_snapshot_ttl fails closed and a click on the still-visible
    restored overlay misses. The reconciler therefore schedules the FIRST
    tick immediately (loop.call_soon), not a full interval out; the tick
    body re-arms the periodic interval as usual."""
    from types import SimpleNamespace

    machine = SimpleNamespace(
        state=OverlayState.PAINTED, pinned_snapshot_id="snap-x",
    )
    controller = _controller(machine=machine)  # type: ignore[arg-type]
    controller.loop = MagicMock()
    controller._reconcile_overlay_keepalive_timer()
    controller.loop.call_soon.assert_called_once_with(
        controller._fire_overlay_keepalive,
    )
    controller.loop.call_later.assert_not_called()


# ---------------------------------------------------------------------------
# wh-overlay-fixqueue-review.1: failed proactive refreshes back off.
#
# A failed refresh restores the prior snapshot by re-entering PAINTED, which
# re-stamps the paint age -- so before this, a window whose walk consistently
# failed was re-walked every window with no back-off, stalling the Input
# process's serial command loop each time. Now each failed PROACTIVE refresh
# doubles the effective trust window (capped), a successful one resets it,
# and closing the session resets it.
# ---------------------------------------------------------------------------


async def _drive_proactive_cycle(*, refresh_ok: bool):
    """Show -> painted over a browser, then one proactive tick and a COMPLETED
    refresh (build+paint ok with a new snapshot, or a failed build restoring
    the prior). Returns (controller, machine, clock, sent, walk_config).

    ``walk_config`` is a mutable dict the walk responder reads on every
    start_overlay_walk: {"ok": bool, "id": str}.
    """
    from services.wheelhouse.shared.overlay_state_changed import (
        OverlayStateChangedEvent,
    )
    from services.wheelhouse.overlay_focus_hooks import ForegroundIdentity

    clock = {"t": 0.0}
    walk_config = {"ok": True, "id": "snap-old"}
    controller = _controller()
    controller.loop = asyncio.get_running_loop()
    controller.background_tasks = []
    controller._overlay_browser_refresh_seconds = 10.0
    controller._overlay_browser_process_set = frozenset({"brave.exe"})
    controller._overlay_now_monotonic = lambda: clock["t"]
    machine = controller.click_overlay_state

    def _respond(action, params):
        sid = params.get("overlay_session_id", 0)
        gen = params.get("paint_generation", 0)
        if action == "start_overlay_walk":
            if not walk_config["ok"]:
                return StartOverlayWalkResponse(
                    status="error", outcome="execution_failed",
                    reason="walk_failed", snapshot_id=None,
                    snapshot_summary=None, trace_id="tr",
                    overlay_session_id=sid, paint_generation=gen,
                ).to_dict()
            return _walk_response(walk_config["id"], sid, gen, 1, 2)
        return _pin_response(action, params)

    sent = _wire_app(controller, _respond)
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value=None
    )
    await controller.handle_overlay_command("show", "tr-bo")
    await _settle(controller)
    ack = OverlayStateChangedEvent(
        state="painted", overlay_session_id=machine.overlay_session_id,
        paint_generation=machine.paint_generation, monitor_ids=(0,),
        snapshot_id="snap-old",
    ).to_dict()
    await controller._handle_overlay_state_changed(ack)
    await _settle(controller)
    assert machine.state is OverlayState.PAINTED
    controller._overlay_tracked_identity = ForegroundIdentity(
        hwnd=11, pid=22, process_name="brave.exe", window_creation_time=1,
    )
    controller._overlay_cancel_keepalive_timer()
    sent.clear()

    # One proactive tick past the trust window.
    walk_config["ok"] = refresh_ok
    walk_config["id"] = "snap-new"
    clock["t"] += 15.0
    controller._fire_overlay_keepalive()
    await _settle(controller)
    controller._overlay_cancel_keepalive_timer()
    assert len(_refresh_walks(sent)) == 1
    if refresh_ok:
        # The build succeeded and pinned snap-new; complete the refresh with
        # the paint ack so the machine swaps back to PAINTED.
        assert machine.state is OverlayState.REFRESH_IN_FLIGHT
        ack2 = OverlayStateChangedEvent(
            state="painted", overlay_session_id=machine.overlay_session_id,
            paint_generation=machine.paint_generation, monitor_ids=(0,),
            snapshot_id="snap-new",
        ).to_dict()
        await controller._handle_overlay_state_changed(ack2)
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
    assert machine.state is OverlayState.PAINTED
    return controller, machine, clock, sent, walk_config


def test_failed_proactive_refresh_doubles_backoff():
    async def _run():
        controller, machine, clock, sent, walk_config = (
            await _drive_proactive_cycle(refresh_ok=False)
        )
        assert controller._overlay_browser_refresh_backoff == 2
        # Within the DOUBLED window (age 15 < 10*2): no new walk fires.
        sent.clear()
        clock["t"] += 15.0
        controller._fire_overlay_keepalive()
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        assert _refresh_walks(sent) == []
        # Past the doubled window (age 30 >= 20): the retry fires.
        sent.clear()
        clock["t"] += 15.0
        controller._fire_overlay_keepalive()
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        assert len(_refresh_walks(sent)) == 1

    asyncio.run(_run())


def test_successful_proactive_refresh_resets_backoff_and_records_swap():
    async def _run():
        controller, machine, clock, sent, walk_config = (
            await _drive_proactive_cycle(refresh_ok=True)
        )
        assert controller._overlay_browser_refresh_backoff == 1
        # The swap guard records the PRIOR snapshot and the swap time.
        swaps = controller._overlay_repaint_swaps
        assert len(swaps) == 1
        prior_id, swap_t = swaps[0]
        assert prior_id == "snap-old"
        assert swap_t == clock["t"]

    asyncio.run(_run())


def test_failed_proactive_refresh_records_no_swap_guard():
    async def _run():
        controller, machine, clock, sent, walk_config = (
            await _drive_proactive_cycle(refresh_ok=False)
        )
        # getattr: since wh-overlay-slow-uia-stale-badges.4 a restore leaves
        # the guard slot alone instead of assigning None to it, and
        # _controller() skips __init__, so on this path nothing ever creates
        # the attribute. Absent and empty both mean no guard.
        assert not getattr(controller, "_overlay_repaint_swaps", None)

    asyncio.run(_run())


def test_a_failed_refresh_after_a_swap_keeps_the_armed_renumber_guard():
    """wh-overlay-slow-uia-stale-badges.4: a restore must not disarm.

    Before this bead the failed-refresh leg assigned None to the guard slot.
    That threw away real protection: the restored badges ARE the ones the
    earlier swap put on screen, so the guard's recorded prior list is still
    the right thing to compare a spoken number against, and a failed refresh
    landing inside the three-second grace window silently reopened the wrong
    click the guard exists to stop.
    """
    async def _run():
        from services.wheelhouse.overlay_focus_hooks import ForegroundIdentity

        controller, machine, clock, sent, walk_config = (
            await _drive_proactive_cycle(refresh_ok=True)
        )
        armed = controller._overlay_repaint_swaps
        assert len(armed) == 1 and armed[0][0] == "snap-old"

        # The successful refresh above was answered with _walk_response,
        # which carries no foreground_* fields, so the controller cleared the
        # tracked identity. Every later proactive tick then stops at the
        # browser-process gate in _maybe_overlay_browser_refresh and feeds no
        # refresh at all. Put the identity back, or this test drives nothing
        # and passes without touching the behaviour it is named for.
        controller._overlay_tracked_identity = ForegroundIdentity(
            hwnd=11, pid=22, process_name="brave.exe",
            window_creation_time=1,
        )

        # A second proactive tick whose walk fails, so the machine restores
        # the list the first refresh had already put on screen.
        walk_config["ok"] = False
        sent.clear()
        clock["t"] += 15.0
        controller._fire_overlay_keepalive()
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        assert len(_refresh_walks(sent)) == 1, (
            "no proactive refresh was fed, so the restore this test is "
            "about never happened and every assertion below is vacuous"
        )
        assert machine.state is OverlayState.PAINTED
        assert machine.pinned_snapshot_id == "snap-new"
        assert controller._overlay_repaint_swaps == armed, (
            "the failed refresh disarmed the renumber guard; the badges it "
            "restored are the post-swap ones, so a number spoken before the "
            "swap can now click the wrong control"
        )

    asyncio.run(_run())


def test_backoff_caps_after_repeated_failures():
    async def _run():
        controller, machine, clock, sent, walk_config = (
            await _drive_proactive_cycle(refresh_ok=False)
        )
        # Drive three more failed proactive refreshes; each fires only once
        # its doubled window has elapsed. 2 -> 4 -> 8 -> capped at 8.
        for expected in (4, 8, 8):
            sent.clear()
            clock["t"] += 10.0 * controller._overlay_browser_refresh_backoff
            controller._fire_overlay_keepalive()
            await _settle(controller)
            controller._overlay_cancel_keepalive_timer()
            assert len(_refresh_walks(sent)) == 1
            assert machine.state is OverlayState.PAINTED
            assert controller._overlay_browser_refresh_backoff == expected

    asyncio.run(_run())


def test_backoff_resets_when_session_closes():
    async def _run():
        controller, machine, clock, sent, walk_config = (
            await _drive_proactive_cycle(refresh_ok=False)
        )
        assert controller._overlay_browser_refresh_backoff == 2
        await controller.handle_overlay_command("hide", "tr-bo-hide")
        await _settle(controller)
        assert machine.state is OverlayState.CLOSED
        assert controller._overlay_browser_refresh_backoff == 1
        assert controller._overlay_repaint_swaps == []

    asyncio.run(_run())



# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.4: the renumber guard arms for EVERY read.
#
# Badge numbers are positional (uia_walker.py increments display_number in
# walk order), so one control added or removed anywhere renumbers everything
# after it. The guard that catches a "click N" spoken across a repaint used to
# arm only for the browser timer's proactive refresh. These tests drive the
# two other repaints that replace the painted list -- a focus-triggered
# refresh and the post-click settle re-read -- over a NON-browser process, and
# the two restores that must NOT arm it.
# ---------------------------------------------------------------------------


async def _drive_repaint_cycle(
    *,
    trigger: str,
    new_snapshot_id: str,
    build_ok: bool = True,
    process_name: str = "claude.exe",
):
    """Show -> painted over ``process_name``, then ONE repaint.

    ``trigger`` is one of:

      * "focus"  -- a focus-hook FOCUS_CHANGE, the ordinary refresh every
        application gets, applied through ``_apply_overlay_event``.
      * "settle" -- a CLICK_COMPLETE with ``settle_after_click`` on: the
        post-click re-read, also through ``_apply_overlay_event``.
      * "show"   -- the user says "show numbers" again while the badges are
        up. This one is applied by ``handle_overlay_command``, which moves
        the machine itself and never calls ``_apply_overlay_event``, so it
        exercises the OTHER apply path.

    Passing the pinned id as ``new_snapshot_id``, or ``build_ok=False``,
    produces a restore of the same painted list instead of a swap.

    ``process_name`` is deliberately not in ``_overlay_browser_process_set``,
    so nothing here can be attributed to the proactive browser refresh.
    Returns (controller, machine, clock, sent).
    """
    from services.wheelhouse.shared.overlay_state_changed import (
        OverlayStateChangedEvent,
    )
    from services.wheelhouse.overlay_focus_hooks import ForegroundIdentity
    from services.wheelhouse.click_overlay_state import (
        OverlayEvent,
        OverlayEventKind,
    )

    clock = {"t": 0.0}
    walk_config = {"ok": True, "id": "snap-old"}
    controller = _controller()
    controller.loop = asyncio.get_running_loop()
    controller.background_tasks = []
    controller._overlay_browser_refresh_seconds = 10.0
    controller._overlay_browser_process_set = frozenset({"brave.exe"})
    controller._overlay_now_monotonic = lambda: clock["t"]
    machine = controller.click_overlay_state
    machine.settle_after_click = trigger == "settle"

    def _respond(action, params):
        sid = params.get("overlay_session_id", 0)
        gen = params.get("paint_generation", 0)
        if action == "start_overlay_walk":
            if not walk_config["ok"]:
                # The schema's clean feature-failure shape: status stays
                # "ok" because the transport worked (shared/
                # start_overlay_walk.py invariant (a) -- status is "error"
                # if and only if outcome is).
                return StartOverlayWalkResponse(
                    status="ok", outcome="execution_failed",
                    reason="walk_failed", snapshot_id=None,
                    snapshot_summary=None, trace_id="tr",
                    overlay_session_id=sid, paint_generation=gen,
                ).to_dict()
            # The four foreground fields are what the settle commit fence
            # compares against the live foreground, so they must name the
            # same window the test keeps in front.
            return StartOverlayWalkResponse(
                status="ok", outcome="ok", reason=None,
                snapshot_id=walk_config["id"],
                snapshot_summary=_summary(walk_config["id"], 1, 2),
                trace_id="tr",
                overlay_session_id=sid, paint_generation=gen,
                foreground_window=11, foreground_pid=22,
                foreground_process_name=process_name,
                foreground_window_creation_time=1,
            ).to_dict()
        return _pin_response(action, params)

    sent = _wire_app(controller, _respond)
    # One stable foreground for the whole cycle. The settle re-read has a
    # commit fence that re-samples the foreground and refuses to paint a
    # reply read from a different window (main.py
    # _overlay_settle_reply_window_still_foreground), so a None here would
    # close the overlay instead of repainting it.
    identity = ForegroundIdentity(
        hwnd=11, pid=22, process_name=process_name, window_creation_time=1,
    )
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value=identity
    )
    await controller.handle_overlay_command("show", "tr-rp")
    await _settle(controller)
    await controller._handle_overlay_state_changed(
        OverlayStateChangedEvent(
            state="painted", overlay_session_id=machine.overlay_session_id,
            paint_generation=machine.paint_generation, monitor_ids=(0,),
            snapshot_id="snap-old",
        ).to_dict()
    )
    await _settle(controller)
    assert machine.state is OverlayState.PAINTED
    controller._overlay_tracked_identity = identity
    controller._overlay_cancel_keepalive_timer()
    sent.clear()

    # The repaint. The clock moves first so an armed guard's timestamp is
    # distinguishable from the paint that preceded it.
    clock["t"] += 5.0
    walk_config["ok"] = build_ok
    walk_config["id"] = new_snapshot_id
    if trigger == "show":
        await controller.handle_overlay_command("show", "tr-rp-reshow")
    else:
        controller._apply_overlay_event(
            OverlayEvent(
                kind=(
                    OverlayEventKind.CLICK_COMPLETE if trigger == "settle"
                    else OverlayEventKind.FOCUS_CHANGE
                ),
            ),
            source=f"test {trigger} repaint",
        )
    await _settle(controller)
    if machine.state is not OverlayState.PAINTED:
        await controller._handle_overlay_state_changed(
            OverlayStateChangedEvent(
                state="painted",
                overlay_session_id=machine.overlay_session_id,
                paint_generation=machine.paint_generation, monitor_ids=(0,),
                snapshot_id=machine.pinned_snapshot_id,
            ).to_dict()
        )
        await _settle(controller)
    controller._overlay_cancel_keepalive_timer()
    assert machine.state is OverlayState.PAINTED
    return controller, machine, clock, sent, walk_config


def test_settle_repaint_on_a_non_browser_app_arms_the_renumber_guard():
    """(a) The post-click re-read swaps the list, so the guard must arm.

    This is the repaint child .2 added: click a badge, the badges clear, the
    application settles, and Wheelhouse reads the screen again. On a
    non-browser process nothing about the browser timer is involved, so a
    guard that keys off the proactive refresh records nothing and a "click N"
    spoken before the repaint resolves against a list the user never saw.
    """
    async def _run():
        controller, machine, clock, sent, walk_config = (
            await _drive_repaint_cycle(
                trigger="settle", new_snapshot_id="snap-new",
            )
        )
        assert machine.pinned_snapshot_id == "snap-new"
        swap = getattr(controller, "_overlay_repaint_swaps", None)
        assert swap, (
            "the settle repaint replaced the painted badges and armed no "
            "renumber guard; a number spoken across it resolves against a "
            "list the user never saw"
        )
        assert len(swap) == 1
        prior_id, swap_t = swap[0]
        assert prior_id == "snap-old"
        assert swap_t == clock["t"]

    asyncio.run(_run())


def test_focus_triggered_refresh_arms_the_renumber_guard():
    """(b) A focus-hook refresh swaps the list on any application."""
    async def _run():
        controller, machine, clock, sent, walk_config = (
            await _drive_repaint_cycle(
                trigger="focus", new_snapshot_id="snap-new",
            )
        )
        assert machine.pinned_snapshot_id == "snap-new"
        swap = getattr(controller, "_overlay_repaint_swaps", None)
        assert swap, (
            "a focus-triggered refresh replaced the painted badges and armed "
            "no renumber guard"
        )
        assert len(swap) == 1
        prior_id, swap_t = swap[0]
        assert prior_id == "snap-old"
        assert swap_t == clock["t"]

    asyncio.run(_run())


def test_settle_repaint_of_the_same_snapshot_arms_nothing():
    """(c) Nothing changed, so the badges are identical and N still means N.

    The Input side answers the settle build with the SAME snapshot id it was
    already holding. Arming here would refuse a number the user can plainly
    still read.
    """
    async def _run():
        controller, machine, clock, sent, walk_config = (
            await _drive_repaint_cycle(
                trigger="settle", new_snapshot_id="snap-old",
            )
        )
        assert machine.pinned_snapshot_id == "snap-old"
        # getattr, not attribute access: _controller() skips __init__, so
        # before the widened guard nothing on this path ever creates the
        # attribute and a bare read would fail for bookkeeping instead of
        # for the behaviour under test.
        assert not getattr(controller, "_overlay_repaint_swaps", None)

    asyncio.run(_run())


def test_failed_focus_refresh_restore_arms_nothing():
    """(c) The failed build restores the prior list, so no badge changed."""
    async def _run():
        controller, machine, clock, sent, walk_config = (
            await _drive_repaint_cycle(
                trigger="focus", new_snapshot_id="snap-new", build_ok=False,
            )
        )
        assert machine.pinned_snapshot_id == "snap-old"
        assert not getattr(controller, "_overlay_repaint_swaps", None)

    asyncio.run(_run())


def test_a_session_close_drops_a_stranded_repaint_entry_pin():
    """(c) A repaint the session closed under must not arm the NEXT session.

    A repaint that ends in CLOSED rather than PAINTED -- the window closed
    under a settle re-read, a refresh that timed out, "hide numbers" said
    while the walk was still out -- leaves the entry pin recorded and never
    consumed. Without the reset the FIRST paint of the next session compares
    its own fresh snapshot against a list from the closed session, arms the
    guard, and refuses a number the user just read off badges that never
    changed under them.

    The stranded pin is set here by hand: the test transport answers every
    walk synchronously, so a build cannot be left in flight across the close.
    """
    async def _run():
        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        controller, machine, clock, sent, walk_config = (
            await _drive_repaint_cycle(
                trigger="focus", new_snapshot_id="snap-new",
            )
        )
        controller._overlay_repaint_entry_pin = "snap-stranded"

        await controller.handle_overlay_command("hide", "tr-rp-hide")
        await _settle(controller)
        assert machine.state is OverlayState.CLOSED

        # A brand new session over the same window, painting a list nothing
        # in the closed session ever showed.
        walk_config["id"] = "snap-third"
        clock["t"] += 5.0
        await controller.handle_overlay_command("show", "tr-rp-show2")
        await _settle(controller)
        await controller._handle_overlay_state_changed(
            OverlayStateChangedEvent(
                state="painted",
                overlay_session_id=machine.overlay_session_id,
                paint_generation=machine.paint_generation, monitor_ids=(0,),
                snapshot_id=machine.pinned_snapshot_id,
            ).to_dict()
        )
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        assert machine.state is OverlayState.PAINTED
        assert machine.pinned_snapshot_id == "snap-third"
        assert not getattr(controller, "_overlay_repaint_swaps", None), (
            "the first paint of a new session armed the renumber guard "
            "against a list from the session that already closed"
        )

    asyncio.run(_run())


def test_a_re_said_show_numbers_swap_arms_the_renumber_guard():
    """A re-said "show numbers" swaps the list, so the guard must arm.

    wh-overlay-slow-uia-stale-badges.4.1.1. This trigger reaches the machine
    through ``handle_overlay_command``, which applies SHOW_NUMBERS itself and
    never calls ``_apply_overlay_event``. The user says "show numbers" and,
    from their memory of the old badges, says "click 16" straight after; the
    re-walk paints a renumbered list before the second transcript arrives.
    Without the entry pin recorded on this path the swap arms nothing and
    that number resolves against a list the user never read.
    """
    async def _run():
        controller, machine, clock, sent, walk_config = (
            await _drive_repaint_cycle(
                trigger="show", new_snapshot_id="snap-new",
            )
        )
        assert machine.pinned_snapshot_id == "snap-new"
        armed = getattr(controller, "_overlay_repaint_swaps", None)
        assert armed and armed[-1][0] == "snap-old", (
            "a re-said 'show numbers' repainted a different list without "
            "arming the renumber guard, so a number spoken against the old "
            "badges resolves against the new ones"
        )

    asyncio.run(_run())


def test_a_second_swap_inside_the_grace_window_keeps_both_priors():
    """Two swaps in one grace window: the guard keeps BOTH priors.

    wh-overlay-slow-uia-stale-badges.4.1.2 asked for the FIRST prior to
    survive the second swap: the list the user read is the one that was on
    screen when they began speaking, and overwriting it compares two lists
    the user never read against each other.
    wh-overlay-slow-uia-stale-badges.4.2.1 then showed that keeping only the
    first loses the user who began speaking while the INTERMEDIATE list was
    up. Both are lists somebody may have read, so both are kept and badge N
    has to be unchanged against each of them.

    Only the widened arming makes this reachable: the browser timer's ticks
    are ``_overlay_browser_refresh_seconds`` apart, far beyond the 3-second
    grace window, so before this bead an armed guard could not be overwritten.
    """
    async def _run():
        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
        )

        controller, machine, clock, sent, walk_config = (
            await _drive_repaint_cycle(
                trigger="focus", new_snapshot_id="snap-new",
            )
        )
        first = getattr(controller, "_overlay_repaint_swaps", None)
        assert len(first) == 1 and first[0][0] == "snap-old"

        # A second swap one second later -- a menu popup, another focus
        # change -- well inside _OVERLAY_RENUMBER_GRACE_SECONDS (3.0).
        walk_config["id"] = "snap-third"
        clock["t"] += 1.0
        controller._apply_overlay_event(
            OverlayEvent(kind=OverlayEventKind.FOCUS_CHANGE),
            source="test second repaint",
        )
        await _settle(controller)
        if machine.state is not OverlayState.PAINTED:
            await controller._handle_overlay_state_changed(
                OverlayStateChangedEvent(
                    state="painted",
                    overlay_session_id=machine.overlay_session_id,
                    paint_generation=machine.paint_generation,
                    monitor_ids=(0,),
                    snapshot_id=machine.pinned_snapshot_id,
                ).to_dict()
            )
            await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        assert machine.state is OverlayState.PAINTED
        assert machine.pinned_snapshot_id == "snap-third"

        second = getattr(controller, "_overlay_repaint_swaps", None)
        assert second, "the second swap disarmed the guard"
        assert second[0] == first[0], (
            "the second swap inside the grace window dropped the first "
            "swap's prior; a number spoken against the list the user was "
            "reading before that swap is now compared against the wrong pair"
        )
        assert len(second) == 2 and second[1][0] == "snap-new", (
            "the second swap did not record the intermediate list; a number "
            "spoken while snap-new was on screen resolves against snap-third "
            "with nothing to compare it to"
        )

    asyncio.run(_run())

# ---------------------------------------------------------------------------
# Config -> state machine wiring (wh-overlay-slow-uia-stale-badges.1)
# ---------------------------------------------------------------------------


def test_overlay_machine_takes_the_settle_flag_from_config():
    """LogicController must not construct the machine at a hard-coded flag.

    The flag ships False. This test proves the config value reaches the
    machine, so turning the key on in config.toml is enough to enable the
    behaviour once child .2 lands.
    """
    from services.wheelhouse.main import make_click_overlay_state_machine
    from services.wheelhouse.ui.click_config import ClickConfig

    on = ClickConfig.from_raw(
        {"enabled": True, "overlay_settle_after_click": True}
    )
    off = ClickConfig.from_raw({"enabled": True})

    assert make_click_overlay_state_machine(on).settle_after_click is True
    assert make_click_overlay_state_machine(off).settle_after_click is False


def test_overlay_machine_deadlines_follow_the_screen_read_key():
    """The factory derives both walk-ish deadlines from the read limit.

    wh-overlay-slow-uia-stale-badges.3. The settle deadline is the
    load-bearing one: at the class default of 8000 a settle read that answers
    at 9 s would be dropped by POST_CLICK_SETTLING's own timeout while the
    Logic awaiter still had a second left, which is the same discard the bead
    removes elsewhere. Both carry the 250 ms pre-walk margin so the machine
    deadline outlasts the Input-side read it is waiting on. The settle one
    also carries ``_OVERLAY_READ_SENTENCE_WAIT_MAX_S`` in milliseconds
    (wh-overlay-slow-uia-stale-badges.3.1.1): its timer is armed BEFORE the
    settle build, and that build waits for the open dictation sentence to end
    before the read is even sent, so without the wait term a read still inside
    its own bound is discarded by the state. ``walk_deadline_ms`` does not get
    the term -- those timers are armed after the build round trip, so the wait
    is already inside their window. ``settle_deadline_ms`` never drops below
    the class default, and ``paint_deadline_ms`` is untouched (a paint is a
    GUI round trip, not a read).
    """
    from services.wheelhouse.main import make_click_overlay_state_machine
    from services.wheelhouse.ui.click_config import ClickConfig

    wait_ms = int(_OVERLAY_READ_SENTENCE_WAIT_MAX_S * 1000)

    m = make_click_overlay_state_machine(
        ClickConfig.from_raw({"enabled": True, "screen_read_timeout_ms": 8000})
    )
    assert m.walk_deadline_ms == 8250
    assert m.settle_deadline_ms == 8250 + wait_ms == 13250

    default = make_click_overlay_state_machine(
        ClickConfig.from_raw({"enabled": True})
    )
    assert default.walk_deadline_ms == 10250
    assert default.settle_deadline_ms == 10250 + wait_ms == 15250
    assert default.paint_deadline_ms == 1000


def _click_complete_effects_for(raw_click_config):
    """Drive a real successful badge click and return the effects it produced.

    Builds the machine the way LogicController does -- through the factory,
    from a validated ClickConfig -- so the assertion covers the whole path
    from the config key to the effects the performer receives, not just the
    machine in isolation (wh-overlay-slow-uia-stale-badges.13.4). The
    performer is replaced by a RECORDER, not a bare mock: the point is to
    read the effects, and a mock that swallows them proves only the state.
    """
    from services.wheelhouse.main import make_click_overlay_state_machine
    from services.wheelhouse.ui.click_config import ClickConfig

    recorded: list = []

    async def _run():
        machine = make_click_overlay_state_machine(
            ClickConfig.from_raw(raw_click_config)
        )
        sid, gen = _drive_to_painted(machine, "snap-cc")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        def _record(effects, *, trace_id):
            recorded.extend(effects)
            return None

        controller._perform_overlay_effects = _record  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        await controller._send_snapshot_item_click(
            snapshot_id="snap-cc", item_id="snap-cc-item-2", trace_id="tr-cc",
            overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        return machine, (sid, gen)

    machine, pair = asyncio.run(_run())
    return machine, pair, recorded


def test_click_complete_effects_reach_the_performer_with_the_flag_off():
    """The shipped path: a successful click dispatches a refresh build."""
    machine, (sid, gen), effects = _click_complete_effects_for(
        {"enabled": True}
    )

    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert machine.overlay_session_id == sid
    assert machine.paint_generation == gen + 1
    assert [e.kind for e in effects] == [
        EffectKind.CANCEL_TIMER,
        EffectKind.DISPATCH_BUILD,
        EffectKind.ARM_TIMER,
    ]
    assert effects[2].timer_state is OverlayState.REFRESH_IN_FLIGHT
    # Still visible, so still pinned.
    assert machine.pinned_snapshot_id == "snap-cc"


def test_keepalive_stays_armed_during_post_click_settling():
    """The settling state retains the pin, so it needs the keepalive too.

    wh-overlay-slow-uia-stale-badges.13.5. Before this fix the reconciler
    permitted only PAINTED and PAUSED, so the successful click that entered
    settling cancelled the keepalive on the same apply. Neither the Logic
    cache nor the Input store was refreshed for up to settle_deadline_ms
    (8000 ms), and a pin does NOT stop TTL expiry in either store: see
    ui/element_finder.py, "TTL evicts PINNED snapshots too". The retained
    snapshot could therefore expire before child .2 read it, while the state
    machine still reported a pin.
    """
    from types import SimpleNamespace

    machine = SimpleNamespace(
        state=OverlayState.POST_CLICK_SETTLING, pinned_snapshot_id="snap-s",
    )
    controller = _controller(machine=machine)  # type: ignore[arg-type]
    controller.loop = MagicMock()

    controller._reconcile_overlay_keepalive_timer()

    controller.loop.call_soon.assert_called_once_with(
        controller._fire_overlay_keepalive,
    )


def test_keepalive_tick_refreshes_both_stores_during_settling():
    """The timer is useless if its callback returns early in this state.

    Two separate state filters guard the keepalive, and both had to change
    (wh-overlay-slow-uia-stale-badges.13.5). This asserts the callback does
    the work: it re-puts the Logic-side summary AND sends the Input-store
    refresh, which are two independently expiring stores.
    """
    from types import SimpleNamespace

    machine = SimpleNamespace(
        state=OverlayState.POST_CLICK_SETTLING,
        pinned_snapshot_id="snap-s",
        overlay_session_id=7,
    )
    controller = _controller(machine=machine)  # type: ignore[arg-type]
    controller.loop = MagicMock()
    controller._overlay_keepalive_summary = MagicMock()  # type: ignore[method-assign]
    controller.create_task_with_error_handling = MagicMock()  # type: ignore[method-assign]
    controller._overlay_send_refresh = MagicMock()  # type: ignore[method-assign]

    controller._fire_overlay_keepalive()

    controller._overlay_keepalive_summary.assert_called_once_with("snap-s")
    controller._overlay_send_refresh.assert_called_once()
    assert controller._overlay_send_refresh.call_args.args[0] == "snap-s"
    # It must reschedule itself, or the refresh happens once and stops.
    controller.loop.call_later.assert_called_once()


def test_click_complete_effects_reach_the_performer_with_the_flag_on():
    """The gated path: the same click clears the badges and arms the settle.

    This is the only test that runs the config key, the factory, the
    controller feed and the effect contract together. The machine-level
    settling tests build ClickOverlayStateMachine(settle_after_click=True)
    by hand, which cannot catch a factory or feed that drops the flag.
    """
    machine, (sid, gen), effects = _click_complete_effects_for(
        {"enabled": True, "overlay_settle_after_click": True}
    )

    assert machine.state is OverlayState.POST_CLICK_SETTLING
    assert [e.kind for e in effects] == [
        EffectKind.CANCEL_TIMER,
        EffectKind.DISPATCH_CLEAR,
        EffectKind.ARM_TIMER,
        EffectKind.DISPATCH_BUILD,
    ]
    assert effects[2].timer_state is OverlayState.POST_CLICK_SETTLING
    # The pin and the SESSION survive: child .2 compares its next read
    # against the list that was on screen, and repaints into this same
    # session. The GENERATION moves, because child .2 spends the bump child
    # .1 reserved on the settle build it dispatches here; the clear keeps
    # the generation the GUI painted.
    assert machine.pinned_snapshot_id == "snap-cc"
    assert machine.overlay_session_id == sid
    assert effects[1].paint_generation == gen
    assert effects[3].paint_generation == gen + 1
    # The timer carries the bumped generation too, so its TIMEOUT survives the
    # machine's generation gate (wh-overlay-slow-uia-stale-badges.2.2.3).
    assert effects[2].paint_generation == gen + 1
    assert machine.paint_generation == gen + 1


# ---------------------------------------------------------------------------
# Concurrent badge clicks (wh-overlay-slow-uia-stale-badges.8).
#
# Every resolved badge click runs as an independent background task and the
# machine stays PAINTED until the Input reply arrives, so before this slice a
# second spoken number could resolve against the SAME old list and be sent
# while the first click was still in flight; Input runs both in order, and
# the second click landed after the screen the user read was gone. Four
# guards close that window:
#
#   1. A badge click carries the (overlay_session_id, paint_generation) pair
#      of the VISIBLE list it was resolved against (the machine's current
#      pair in PAINTED; the pair of the last successful paint during
#      REFRESH_IN_FLIGHT), tracked by
#      ``_reconcile_overlay_visible_painted_pair``.
#   2. While one badge click is in flight, a second one is REFUSED with an
#      overlay_click_in_flight notice (never silence, never a second send).
#   3. A successful click cancels a held 'click N' armed at the same pair
#      (the only not-yet-sent click Logic queues) with a numbers_updating
#      notice instead of letting it resolve against the consumed list.
#   4. Input refuses a click whose resolved-against pair is not newer than
#      the last EXECUTED click's pair (stale_overlay_generation) -- the
#      Input-side tests live in test_ui/test_click_snapshot_item_handler.py;
#      the reason's membership in the rewalk set is asserted here.
# ---------------------------------------------------------------------------


def test_second_click_refused_while_first_in_flight():
    """THE guard test (acceptance c): two clicks driven concurrently; the
    second is refused with an understandable notice and no second send."""

    async def _run():
        machine = ClickOverlayStateMachine()
        _drive_to_painted(machine, "snap-if")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        release = asyncio.Event()
        sent: list[tuple[str, dict]] = []

        async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
            sent.append((action, dict(params or {})))
            await release.wait()
            return _click_response("ok")

        controller.app = MagicMock()
        controller.app.send_request = _send_request

        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-if", item_id="snap-if-item-1", trace_id="tr-a",
        )
        await asyncio.sleep(0)  # first send starts and blocks on the event
        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-if", item_id="snap-if-item-2", trace_id="tr-b",
        )
        await asyncio.sleep(0)

        notice = cast(MagicMock, controller._forward_click_notice)
        refused_calls = list(notice.call_args_list)
        first_sent_count = len(sent)

        release.set()
        await asyncio.gather(
            *controller.background_tasks, return_exceptions=True
        )
        await asyncio.sleep(0)
        return controller, sent, refused_calls, first_sent_count

    controller, sent, refused_calls, first_sent_count = asyncio.run(_run())
    # Only the FIRST click reached Input, both before and after the reply.
    assert first_sent_count == 1
    assert len(sent) == 1
    # The second number got a message, not silence.
    assert len(refused_calls) == 1
    _, kwargs = refused_calls[0]
    assert kwargs.get("outcome") == "execution_failed"
    assert kwargs.get("reason") == "overlay_click_in_flight"
    # The in-flight marker is cleared once the reply arrives.
    assert controller._overlay_click_in_flight is None


def test_next_click_dispatches_after_ok_reply():
    """The guard is per-flight, not a latch: after the first reply the next
    spoken number dispatches normally."""

    async def _run():
        machine = ClickOverlayStateMachine()
        _drive_to_painted(machine, "snap-seq")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        sent = _wire_app(controller, lambda a, p: _click_response("ok"))

        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-seq", item_id="snap-seq-item-1", trace_id="tr-1",
        )
        await asyncio.gather(
            *controller.background_tasks, return_exceptions=True
        )
        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-seq", item_id="snap-seq-item-2", trace_id="tr-2",
        )
        await asyncio.gather(
            *controller.background_tasks, return_exceptions=True
        )
        return sent

    sent = asyncio.run(_run())
    assert len(sent) == 2


@pytest.mark.parametrize(
    "responder",
    [
        "timeout",
        "send-failure",
        "malformed",
        "non-ok",
    ],
)
def test_in_flight_guard_clears_on_every_failure_path(responder):
    """A timeout, a send failure, a malformed reply, and a refused click must
    all clear the in-flight marker, or one bad reply locks out every later
    badge click for the session."""

    async def _run():
        machine = ClickOverlayStateMachine()
        _drive_to_painted(machine, "snap-fl")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
            if responder == "timeout":
                raise asyncio.TimeoutError()
            if responder == "send-failure":
                raise RuntimeError("boom")
            if responder == "malformed":
                return {"garbage": True}
            return _click_response(
                "execution_failed", reason="invoke_com_error",
            )

        controller.app = MagicMock()
        controller.app.send_request = _send_request

        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-fl", item_id="snap-fl-item-1", trace_id="tr-f",
        )
        await asyncio.gather(
            *controller.background_tasks, return_exceptions=True
        )
        return controller

    controller = asyncio.run(_run())
    assert controller._overlay_click_in_flight is None


def test_dispatch_carries_resolved_pair_to_input():
    """Part 1: the click_snapshot_item params carry the (overlay_session_id,
    paint_generation) pair of the visible list the number was resolved
    against, so Input can refuse a click from a consumed list."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-pair")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        sent = _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._overlay_visible_painted_pair = (sid, gen)

        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-pair", item_id="snap-pair-item-1",
            trace_id="tr-p",
        )
        await asyncio.gather(
            *controller.background_tasks, return_exceptions=True
        )
        return sent, (sid, gen)

    sent, (sid, gen) = asyncio.run(_run())
    assert sent[0][0] == "click_snapshot_item"
    params = sent[0][1]
    assert params["overlay_session_id"] == sid
    assert params["paint_generation"] == gen


def test_dispatch_without_resolved_pair_omits_the_fields():
    """With no recorded visible pair (defensive controller, legacy paths) the
    payload keeps its pre-slice shape so Input skips the stale check."""

    async def _run():
        machine = ClickOverlayStateMachine()
        _drive_to_painted(machine, "snap-nop")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        sent = _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._overlay_visible_painted_pair = None

        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-nop", item_id="snap-nop-item-1",
            trace_id="tr-n",
        )
        await asyncio.gather(
            *controller.background_tasks, return_exceptions=True
        )
        return sent

    sent = asyncio.run(_run())
    params = sent[0][1]
    assert "overlay_session_id" not in params
    assert "paint_generation" not in params


def test_reconcile_visible_painted_pair_follows_machine():
    """The reconcile records the pair on PAINTED, KEEPS it while the same
    list stays visible (refresh in flight), and clears it once nothing the
    user can see remains (closed)."""

    controller = _controller()
    machine = controller.click_overlay_state
    controller._overlay_visible_painted_pair = None

    controller._reconcile_overlay_visible_painted_pair()
    assert controller._overlay_visible_painted_pair is None

    sid, gen = _drive_to_painted(machine, "snap-vp")
    controller._reconcile_overlay_visible_painted_pair()
    assert controller._overlay_visible_painted_pair == (sid, gen)

    # A focus refresh bumps the machine's generation, but the list the user
    # still SEES was painted at the old pair -- the reconcile must keep it.
    machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    controller._reconcile_overlay_visible_painted_pair()
    assert controller._overlay_visible_painted_pair == (sid, gen)

    machine.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
    assert machine.state is OverlayState.CLOSED
    controller._reconcile_overlay_visible_painted_pair()
    assert controller._overlay_visible_painted_pair is None


def test_apply_overlay_event_reconciles_visible_painted_pair():
    """The reconcile is wired into the _apply_overlay_event tail: a hide that
    closes the session clears the recorded pair without a manual call."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-wire")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._overlay_visible_painted_pair = (sid, gen)

        controller._apply_overlay_event(
            OverlayEvent(kind=OverlayEventKind.HIDE_NUMBERS),
            source="test hide",
        )
        return controller

    controller = asyncio.run(_run())
    assert controller._overlay_visible_painted_pair is None


def test_ok_click_cancels_held_click_from_same_generation():
    """Part 3: a successful badge click cancels the held 'click N' armed at
    the same pair -- the only not-yet-sent click Logic queues -- and tells
    the user, instead of letting it resolve against the consumed list."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-hc")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        # A 'click 17' held while the first click's reply was outstanding
        # (e.g. spoken during a mic pause at the same pair).
        controller._hold_click_n(
            number=17, spoken="17", trace_id="tr-held",
        )
        assert controller._overlay_hold_timer is not None

        await controller._send_snapshot_item_click(
            snapshot_id="snap-hc", item_id="snap-hc-item-2",
            trace_id="tr-ok", overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        return controller

    controller = asyncio.run(_run())
    # The held click was cancelled...
    assert controller._overlay_hold_timer is None
    # ...and the user was told (never a silent drop).
    notice = cast(MagicMock, controller._forward_click_notice)
    notice.assert_called_once()
    _, kwargs = notice.call_args
    assert kwargs.get("reason") == "numbers_updating"
    assert kwargs.get("spoken_name") == "17"


def test_ok_click_keeps_held_click_from_different_generation():
    """A held click armed at a NEWER pair (a supersede happened after the
    click was dispatched) belongs to the newer overlay; the old click's
    success must not cancel it."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-hk")
        # Supersede AFTER the click was dispatched at (sid, gen).
        machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        _wire_app(controller, lambda a, p: _click_response("ok"))
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        # Held at the machine's CURRENT (bumped) pair.
        controller._hold_click_n(
            number=3, spoken="3", trace_id="tr-held2",
        )
        held_timer = controller._overlay_hold_timer
        assert held_timer is not None

        await controller._send_snapshot_item_click(
            snapshot_id="snap-hk", item_id="snap-hk-item-2",
            trace_id="tr-ok2", overlay_dispatch_pair=(sid, gen),
        )
        await asyncio.sleep(0)
        still_armed = controller._overlay_hold_timer is not None
        # Clean up the timer so nothing fires after the loop closes.
        if controller._overlay_hold_timer is not None:
            controller._overlay_hold_timer.cancel()
        return still_armed

    still_armed = asyncio.run(_run())
    assert still_armed


def test_stale_overlay_generation_is_a_rewalk_refusal_reason():
    """Input's stale_overlay_generation refusal means a click already
    consumed the list the badge came from -- the screen has changed, so a
    re-walk is the recovery that brings usable numbers back without another
    spoken command. A DIRECT literal assertion, mirroring
    test_taskbar_closed_is_a_rewalk_refusal_reason (the parametrized rewalk
    test cannot catch an omission from the set itself)."""
    assert "stale_overlay_generation" in _OVERLAY_REWALK_REFUSAL_REASONS


# ---------------------------------------------------------------------------
# Review follow-ups (wh-overlay-slow-uia-stale-badges.16).
#
# .16.1: a FAILED refresh falls back to PAINTED with the machine's bumped
# generation but the SAME still-visible list (the fall-back restores the
# prior pin, not the prior generation). Recording the bumped pair there
# would re-admit the consumed list under a new generation and defeat the
# Input watermark: the next click on the same physical list would carry a
# pair strictly newer than the watermark and EXECUTE against a screen the
# first click already changed. The reconciler therefore memoizes WHICH
# pinned snapshot its recorded pair belongs to, and on PAINTED entry keeps
# the pair when the machine's pin is that same snapshot (fall-back restore,
# paused-resume repaint) -- recording only for a genuinely fresh list.
#
# .16.3: coverage gaps -- the REFRESH_IN_FLIGHT payload/dispatch-pair
# divergence, the hold timer firing mid-flight, the GUI (mouse) badge-click
# path against the new guards, and the two untested in-flight-marker exits.
# ---------------------------------------------------------------------------


def _drive_painted_to_refresh(controller, snapshot_id: str):
    """PAINTED at (sid, gen) -> FOCUS_CHANGE -> REFRESH_IN_FLIGHT (gen+1).

    Reconciles the visible-pair tracker after each apply, mirroring the
    ``_apply_overlay_event`` tail. Returns (sid, gen) -- the VISIBLE list's
    pair, one behind the machine.
    """

    machine = controller.click_overlay_state
    sid, gen = _drive_to_painted(machine, snapshot_id)
    controller._reconcile_overlay_visible_painted_pair()
    assert controller._overlay_visible_painted_pair == (sid, gen)
    machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert machine.paint_generation == gen + 1
    controller._reconcile_overlay_visible_painted_pair()
    assert controller._overlay_visible_painted_pair == (sid, gen)
    return sid, gen


def test_refresh_fall_back_keeps_the_recorded_visible_pair():
    """wh-overlay-slow-uia-stale-badges.16.1 (gap 5): a failed refresh BUILD
    falls back to PAINTED with the OLD list still on screen. The reconciler
    must KEEP the recorded pair (S, g), not record the bumped (S, g+1) --
    the bumped pair would beat Input's executed-click watermark and let a
    click on the consumed list execute against an already-changed screen.

    Finding .18.7 removed the former paint-failed leg of this test: a
    failed refresh PAINT without auto-hide no longer falls back (the
    manager destroyed part of the display, so the machine closes; see
    test_refresh_failed_paint_ack_closes_clears_and_stops_lease_renew).
    Only the build failure keeps the non-destructive fall-back here."""

    controller = _controller()
    machine = controller.click_overlay_state
    controller._overlay_visible_painted_pair = None

    sid, gen = _drive_painted_to_refresh(controller, "snap-fb")

    machine.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
            paint_generation=gen + 1, snapshot_id=None, build_ok=False,
        )
    )
    assert machine.state is OverlayState.PAINTED
    # The fall-back kept the prior pin: the same physical list.
    assert machine.pinned_snapshot_id == "snap-fb"

    controller._reconcile_overlay_visible_painted_pair()
    assert controller._overlay_visible_painted_pair == (sid, gen)


def test_refresh_fall_back_via_paused_keeps_the_recorded_visible_pair():
    """The auto-hide leg of gap 5: a refresh that fails while a mic pause is
    in flight falls back to PAUSED, and MIC_RESUME(snapshot_valid=True) goes
    DIRECTLY back to PAINTED with the restored pin. The memo must survive
    PAUSED so that re-entry keeps the consumed list's pair too.

    Finding .18.7 KEEPS this leg: the mic-pause already cleared the screen
    and PAUSED stops the keepalive renew, so the auto-hide fall-back stays
    bounded (only the non-auto-hide failed paint now closes)."""

    controller = _controller()
    machine = controller.click_overlay_state
    controller._overlay_visible_painted_pair = None

    sid, gen = _drive_painted_to_refresh(controller, "snap-ah")
    machine.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
            paint_generation=gen + 1, snapshot_id="snap-ah-new",
        )
    )
    machine.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    machine.apply(
        OverlayEvent(
            OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
            paint_generation=gen + 1, paint_state=PaintAckState.FAILED,
        )
    )
    assert machine.state is OverlayState.PAUSED
    assert machine.pinned_snapshot_id == "snap-ah"
    controller._reconcile_overlay_visible_painted_pair()
    assert controller._overlay_visible_painted_pair == (sid, gen)

    machine.apply(
        OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True)
    )
    assert machine.state is OverlayState.PAINTED
    controller._reconcile_overlay_visible_painted_pair()
    assert controller._overlay_visible_painted_pair == (sid, gen)


def test_successful_refresh_records_the_fresh_pair():
    """The other half of the gap-5 discriminator: a refresh that SUCCEEDS
    paints a genuinely new list (the machine pins the NEW snapshot id), so
    the reconciler must RECORD the bumped pair -- keeping the old one there
    would refuse the new list's first click as stale."""

    controller = _controller()
    machine = controller.click_overlay_state
    controller._overlay_visible_painted_pair = None

    sid, gen = _drive_painted_to_refresh(controller, "snap-fr")
    machine.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
            paint_generation=gen + 1, snapshot_id="snap-fr-new",
        )
    )
    machine.apply(
        OverlayEvent(
            OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
            paint_generation=gen + 1, paint_state=PaintAckState.PAINTED,
        )
    )
    assert machine.state is OverlayState.PAINTED
    assert machine.pinned_snapshot_id == "snap-fr-new"

    controller._reconcile_overlay_visible_painted_pair()
    assert controller._overlay_visible_painted_pair == (sid, gen + 1)


def test_click_during_refresh_diverges_payload_pair_from_dispatch_pair():
    """Gap 1 (wh-overlay-slow-uia-stale-badges.16.3): during
    REFRESH_IN_FLIGHT the two pairs a dispatch captures DIVERGE -- the
    payload must carry the kept last-painted pair (what the user read),
    while overlay_dispatch_pair is the machine's bumped current pair (what
    gates the CLICK_COMPLETE feed). A dispatcher that passed the dispatch
    pair as the resolved pair would consume the NEW generation and wrongly
    refuse the new list's first click."""

    async def _run():
        controller = _controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        sid, gen = _drive_painted_to_refresh(controller, "snap-dv")
        sent = _wire_app(controller, lambda a, p: _click_response("ok"))

        captured: dict = {}
        real_send = controller._send_snapshot_item_click

        async def _spy(**kwargs):
            captured.update(kwargs)
            await real_send(**kwargs)

        controller._send_snapshot_item_click = _spy  # type: ignore[method-assign]

        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-dv", item_id="snap-dv-item-1",
            trace_id="tr-dv",
        )
        await asyncio.gather(
            *controller.background_tasks, return_exceptions=True
        )
        return captured, sent, sid, gen

    captured, sent, sid, gen = asyncio.run(_run())
    # The payload carries the VISIBLE list's pair...
    assert sent[0][0] == "click_snapshot_item"
    params = sent[0][1]
    assert params["overlay_session_id"] == sid
    assert params["paint_generation"] == gen
    assert captured["overlay_resolved_pair"] == (sid, gen)
    # ...while the dispatch pair is the machine's bumped current pair.
    assert captured["overlay_dispatch_pair"] == (sid, gen + 1)


def test_hold_timer_firing_mid_flight_is_refused_not_sent():
    """Gap 2: a held 'click N' whose timer fires while a badge click's reply
    is still outstanding must hit the in-flight guard -- one refusal notice,
    no second send."""

    async def _run():
        machine = ClickOverlayStateMachine()
        _drive_to_painted(machine, "snap-hm")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        controller.click_snapshot_summary_cache.put(
            "snap-hm", _summary("snap-hm", 1, 2, 3)
        )

        release = asyncio.Event()
        sent: list[tuple[str, dict]] = []

        async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
            sent.append((action, dict(params or {})))
            await release.wait()
            return _click_response("ok")

        controller.app = MagicMock()
        controller.app.send_request = _send_request

        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-hm", item_id="snap-hm-item-1",
            trace_id="tr-first",
        )
        await asyncio.sleep(0)  # the first send starts and blocks
        assert controller._overlay_click_in_flight is not None

        controller._hold_click_n(number=2, spoken="2", trace_id="tr-hold")
        # Let the real 200ms hold timer fire while the reply is outstanding.
        await asyncio.sleep(0.25)

        notice = cast(MagicMock, controller._forward_click_notice)
        refused_calls = list(notice.call_args_list)
        sent_while_in_flight = len(sent)

        release.set()
        await asyncio.gather(
            *controller.background_tasks, return_exceptions=True
        )
        return controller, sent, refused_calls, sent_while_in_flight

    controller, sent, refused_calls, sent_while_in_flight = asyncio.run(_run())
    # The held click resolved into the guard: refused, never sent.
    assert sent_while_in_flight == 1
    assert len(sent) == 1
    assert len(refused_calls) == 1
    _, kwargs = refused_calls[0]
    assert kwargs.get("outcome") == "execution_failed"
    assert kwargs.get("reason") == "overlay_click_in_flight"
    # The hold is fully disarmed; nothing re-fires after the refusal.
    assert controller._overlay_hold_timer is None
    assert controller._overlay_hold_armed is None
    assert controller._overlay_click_in_flight is None


def test_mouse_badge_click_refused_while_click_in_flight():
    """Gap 3a: the GUI (mouse) badge-click path routes through the SAME
    dispatcher, so a mouse click while a previous click awaits its reply is
    refused with the overlay_click_in_flight notice and never sent. This is
    why the refusal copy must not say 'say the number' (finding .16.2)."""

    async def _run():
        machine = ClickOverlayStateMachine()
        _drive_to_painted(machine, "snap-ms")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        controller.click_snapshot_summary_cache.put(
            "snap-ms", _summary("snap-ms", 1, 2, 3)
        )

        release = asyncio.Event()
        sent: list[tuple[str, dict]] = []

        async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
            sent.append((action, dict(params or {})))
            await release.wait()
            return _click_response("ok")

        controller.app = MagicMock()
        controller.app.send_request = _send_request

        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-ms", item_id="snap-ms-item-1",
            trace_id="tr-voice",
        )
        await asyncio.sleep(0)  # the first send starts and blocks

        from services.wheelhouse.shared.snapshot_item_clicked import (
            SnapshotItemClickedEvent,
        )

        command = SnapshotItemClickedEvent(
            snapshot_id="snap-ms", display_number=2
        ).to_dict()
        await controller._handle_snapshot_item_clicked(command)

        notice = cast(MagicMock, controller._forward_click_notice)
        refused_calls = list(notice.call_args_list)
        sent_while_in_flight = len(sent)

        release.set()
        await asyncio.gather(
            *controller.background_tasks, return_exceptions=True
        )
        return controller, sent, refused_calls, sent_while_in_flight

    controller, sent, refused_calls, sent_while_in_flight = asyncio.run(_run())
    assert sent_while_in_flight == 1
    assert len(sent) == 1
    assert len(refused_calls) == 1
    _, kwargs = refused_calls[0]
    assert kwargs.get("outcome") == "execution_failed"
    assert kwargs.get("reason") == "overlay_click_in_flight"
    assert controller._overlay_click_in_flight is None


def test_mouse_badge_click_carries_the_visible_pair():
    """Gap 3b: a mouse badge click during REFRESH_IN_FLIGHT resolves against
    the still-visible prior list, so its payload must carry the kept
    last-painted pair -- exactly like the voice path."""

    async def _run():
        controller = _controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
        sid, gen = _drive_painted_to_refresh(controller, "snap-mp")
        controller.click_snapshot_summary_cache.put(
            "snap-mp", _summary("snap-mp", 1, 2, 3)
        )
        sent = _wire_app(controller, lambda a, p: _click_response("ok"))

        from services.wheelhouse.shared.snapshot_item_clicked import (
            SnapshotItemClickedEvent,
        )

        command = SnapshotItemClickedEvent(
            snapshot_id="snap-mp", display_number=2
        ).to_dict()
        await controller._handle_snapshot_item_clicked(command)
        await asyncio.gather(
            *controller.background_tasks, return_exceptions=True
        )
        return sent, sid, gen

    sent, sid, gen = asyncio.run(_run())
    assert sent[0][0] == "click_snapshot_item"
    params = sent[0][1]
    assert params["item_id"] == "snap-mp-item-2"
    assert params["overlay_session_id"] == sid
    assert params["paint_generation"] == gen


def test_cancelled_click_task_clears_the_in_flight_marker():
    """Gap 4a: shutdown (or any cancel) of the in-flight send task must run
    the wrapper's finally and clear the marker -- a wedged marker would
    refuse every later badge click for the session."""

    async def _run():
        machine = ClickOverlayStateMachine()
        _drive_to_painted(machine, "snap-cx")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
        controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]

        never = asyncio.Event()

        async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
            await never.wait()
            return _click_response("ok")

        controller.app = MagicMock()
        controller.app.send_request = _send_request

        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-cx", item_id="snap-cx-item-1",
            trace_id="tr-cx",
        )
        await asyncio.sleep(0)  # the send starts and blocks
        assert controller._overlay_click_in_flight is not None

        task = next(t for t in controller.background_tasks if not t.done())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return controller

    controller = asyncio.run(_run())
    assert controller._overlay_click_in_flight is None


def test_task_creation_failure_clears_the_in_flight_marker():
    """Gap 4b: when task creation itself raises, the send coroutine never
    runs and its finally never executes; the dispatcher's except must clear
    the marker before re-raising."""

    machine = ClickOverlayStateMachine()
    _drive_to_painted(machine, "snap-tf")
    controller = _controller(machine=machine)
    controller._forward_click_notice = MagicMock()  # type: ignore[method-assign]
    controller.create_task_with_error_handling = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("loop is closed")
    )

    with pytest.raises(RuntimeError, match="loop is closed"):
        controller._dispatch_snapshot_item_click(
            snapshot_id="snap-tf", item_id="snap-tf-item-1",
            trace_id="tr-tf",
        )

    assert controller._overlay_click_in_flight is None
    # Close the coroutine the failed create received, so the test does not
    # leak a never-awaited coroutine warning.
    create_mock = cast(MagicMock, controller.create_task_with_error_handling)
    create_mock.call_args[0][0].close()


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.9: the pending-clear watchdog (Shape A),
# the GUI badge-lease renew (Shape B, Logic side), the expired ack, and the
# startup reset. Logic must REPORT a lost clear, not stay silent; the GUI
# lease is the remover.
# ---------------------------------------------------------------------------


def test_put_overlay_gui_action_returns_enqueue_outcome():
    """The clear watchdog needs to know whether the message actually left
    Logic, so the defensive enqueue helper must report success/failure."""

    controller = _controller()
    assert (
        controller._put_overlay_gui_action({"action": "x"}, trace_id="tr")
        is True
    )

    class _Rejecting:
        def put_nowait(self, item):
            raise RuntimeError("queue full")

    controller.state_manager.state_to_gui_queue = _Rejecting()
    assert (
        controller._put_overlay_gui_action({"action": "x"}, trace_id="tr")
        is False
    )

    controller.state_manager = None
    assert (
        controller._put_overlay_gui_action({"action": "x"}, trace_id="tr")
        is False
    )


def test_hide_dispatch_arms_pending_clear_deadline():
    """Dispatching a clear arms the ack watchdog at the clear's pair."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-wd")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, _pin_response)

        await controller.handle_overlay_command("hide", "tr-wd")
        await _settle(controller)

        pending = controller._overlay_pending_clear
        timer = controller._overlay_pending_clear_timer
        if timer is not None:
            timer.cancel()
        return pending, timer, (sid, gen)

    pending, timer, pair = asyncio.run(_run())
    assert pending == pair
    assert timer is not None


def test_cleared_ack_resolves_pending_clear():
    """The GUI's cleared ack at the pending pair disarms the watchdog."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-wr")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, _pin_response)

        await controller.handle_overlay_command("hide", "tr-wr")
        await _settle(controller)
        assert controller._overlay_pending_clear == (sid, gen)

        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        ack = OverlayStateChangedEvent(
            state="cleared", overlay_session_id=sid, paint_generation=gen,
        ).to_dict()
        await controller._handle_overlay_state_changed(ack)
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    assert controller._overlay_pending_clear is None
    assert controller._overlay_pending_clear_timer is None


def test_note_clear_ack_pair_ordering():
    """Dispatch is pair-monotonic, so an ack at a STRICTLY greater pair
    proves the pending clear was consumed; an OLDER ack proves nothing."""

    controller = _controller()
    timer = MagicMock()
    controller._overlay_pending_clear = (2, 3)
    controller._overlay_pending_clear_timer = timer

    # An older ack proves nothing.
    controller._overlay_note_clear_ack(2, 2, "painted")
    assert controller._overlay_pending_clear == (2, 3)
    timer.cancel.assert_not_called()

    # A STRICTLY LATER ack resolves (it comes from a message enqueued
    # after the pending clear, so the clear was consumed first).
    controller._overlay_note_clear_ack(2, 4, "painted")
    assert controller._overlay_pending_clear is None
    timer.cancel.assert_called_once()
    assert controller._overlay_pending_clear_timer is None


def test_equal_pair_painted_ack_does_not_resolve_pending_clear(caplog):
    """A painted ack at the pending clear's OWN pair proves nothing: the
    GUI can send it BEFORE it consumes the clear (a slow walk paints at
    the pair the clear targets). The watchdog must stay armed and the
    deadline must still report (wh-overlay-slow-uia-stale-badges.18.1)."""

    controller = _controller()
    timer = MagicMock()
    controller._overlay_pending_clear = (2, 3)
    controller._overlay_pending_clear_timer = timer

    controller._overlay_note_clear_ack(2, 3, "painted")
    assert controller._overlay_pending_clear == (2, 3)
    timer.cancel.assert_not_called()

    # The deadline still fires and reports the unconfirmed clear.
    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller._fire_overlay_clear_ack_deadline(2, 3, "tr-eqp")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "did not confirm" in errors[0].getMessage()


def test_equal_pair_failed_ack_does_not_resolve_pending_clear():
    """A failed ack at the pending pair is the same pre-consumption case
    as painted: the GUI can send it before the clear is consumed."""

    controller = _controller()
    timer = MagicMock()
    controller._overlay_pending_clear = (2, 3)
    controller._overlay_pending_clear_timer = timer

    controller._overlay_note_clear_ack(2, 3, "failed")
    assert controller._overlay_pending_clear == (2, 3)
    timer.cancel.assert_not_called()


def test_equal_pair_cleared_ack_resolves_pending_clear():
    """A cleared ack at the pending pair proves the badges at that pair
    are GONE (whichever same-pair clear removed them), which is the
    guarantee the watchdog audits."""

    controller = _controller()
    timer = MagicMock()
    controller._overlay_pending_clear = (2, 3)
    controller._overlay_pending_clear_timer = timer

    controller._overlay_note_clear_ack(2, 3, "cleared")
    assert controller._overlay_pending_clear is None
    timer.cancel.assert_called_once()
    assert controller._overlay_pending_clear_timer is None


def test_equal_pair_expired_ack_resolves_pending_clear():
    """An expired ack at the pending pair proves the GUI's lease tore the
    badges down itself -- gone is gone, so the slot resolves."""

    controller = _controller()
    timer = MagicMock()
    controller._overlay_pending_clear = (2, 3)
    controller._overlay_pending_clear_timer = timer

    controller._overlay_note_clear_ack(2, 3, "expired")
    assert controller._overlay_pending_clear is None
    timer.cancel.assert_called_once()
    assert controller._overlay_pending_clear_timer is None


def test_strictly_newer_painted_ack_resolves_pending_clear():
    """A painted ack at a strictly greater pair comes from a message
    enqueued AFTER the pending clear (pair-monotonic dispatch), so the
    FIFO GUI queue consumed the clear first -- any state resolves."""

    controller = _controller()
    timer = MagicMock()
    controller._overlay_pending_clear = (2, 3)
    controller._overlay_pending_clear_timer = timer

    controller._overlay_note_clear_ack(2, 4, "painted")
    assert controller._overlay_pending_clear is None
    timer.cancel.assert_called_once()
    assert controller._overlay_pending_clear_timer is None


def test_clear_ack_deadline_fire_reports_error_once(caplog):
    """A missed clear ack is REPORTED: one ERROR log (the Logic-side error
    notifier pops it to the user), and the watchdog disarms. A stale fire
    (the pending pair moved on) reports nothing."""

    controller = _controller()
    controller._overlay_pending_clear = (2, 3)

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller._fire_overlay_clear_ack_deadline(2, 3, "tr-dl")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "did not confirm" in errors[0].getMessage()
    assert controller._overlay_pending_clear is None

    # A stale fire (nothing pending, or a different pair) is silent.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller._fire_overlay_clear_ack_deadline(2, 3, "tr-dl2")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_deadline_fire_under_shutdown_clears_slot_without_reporting():
    """wh-overlay-slow-uia-stale-badges.18.9: shutdown() cancels the
    watchdog timer, but the callback can already be queued on the loop
    when the shutdown signal arrives. The race-safe backstop: with the
    shutdown event set, the fire still clears the pending slot state but
    reports NOTHING -- an intentional exit is not an overlay fault."""

    controller = _controller()
    controller._overlay_pending_clear = (2, 3)
    controller.shutdown_event = MagicMock()
    controller.shutdown_event.is_set.return_value = True
    controller._report_overlay_clear_fault = MagicMock()  # type: ignore[method-assign]

    controller._fire_overlay_clear_ack_deadline(2, 3, "tr-sd")

    controller._report_overlay_clear_fault.assert_not_called()
    assert controller._overlay_pending_clear is None
    assert controller._overlay_pending_clear_timer is None


def test_deadline_fault_message_advises_apply_numbers(caplog):
    """The deadline fault's advice must work from the CLOSED state the
    primary trigger (a hide's clear) leaves the machine in: HIDE_NUMBERS
    in CLOSED is a NO_OP, so 'hide numbers' would retry nothing. The
    advice is 'show numbers' (wh-overlay-slow-uia-stale-badges.18.3)."""

    controller = _controller()
    controller._overlay_pending_clear = (2, 3)

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller._fire_overlay_clear_ack_deadline(2, 3, "tr-adv")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "hide numbers" not in message
    assert "show numbers" in message


def test_clear_enqueue_failure_reports_error_immediately(caplog):
    """A clear that never left Logic (queue rejected the put) is reported
    at once -- there is nothing to wait for. A repeat fault at the SAME
    pair downgrades to WARNING so one broken pair cannot storm popups."""

    controller = _controller()

    class _Rejecting:
        def put_nowait(self, item):
            raise RuntimeError("queue full")

    controller.state_manager.state_to_gui_queue = _Rejecting()
    effect = Effect(
        kind=EffectKind.DISPATCH_CLEAR, overlay_session_id=2,
        paint_generation=3,
    )

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        asyncio.run(controller._overlay_dispatch_clear_one(effect, "tr-ef"))
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "could not be handed" in errors[0].getMessage()
    assert controller._overlay_pending_clear is None
    assert controller._overlay_pending_clear_timer is None

    # Same pair again: WARNING, not a second ERROR popup.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        asyncio.run(controller._overlay_dispatch_clear_one(effect, "tr-ef2"))
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


def test_keepalive_renews_gui_lease_while_painted():
    """Each keepalive tick in PAINTED renews the GUI badge lease with
    lease_ms = max(floor, ticks * interval), so a healthy Logic never lets
    the GUI-side lease fire."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-lr")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, _pin_response)

        _gui_items(controller).clear()
        controller._fire_overlay_keepalive()
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        return controller, (sid, gen)

    controller, pair = asyncio.run(_run())
    renews = [
        m for m in _gui_items(controller)
        if m.get("action") == "overlay_lease_renew"
    ]
    assert len(renews) == 1
    assert renews[0]["overlay_session_id"] == pair[0]
    assert renews[0]["paint_generation"] == pair[1]
    # interval 15s * 4 ticks = 60000ms, equal to the floor.
    assert renews[0]["lease_ms"] == 60000


def test_keepalive_sends_no_renew_outside_painted():
    """PAUSED and POST_CLICK_SETTLING are keepalive states (the pin needs
    its TTL slid) but have NO badges on screen, so no lease is renewed --
    the GUI already cancelled it at the clear."""

    async def _run():
        machine = ClickOverlayStateMachine()
        _drive_to_painted(machine, "snap-lp")
        machine.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
        assert machine.state is OverlayState.PAUSED
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, _pin_response)

        _gui_items(controller).clear()
        controller._fire_overlay_keepalive()
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        return controller

    controller = asyncio.run(_run())
    renews = [
        m for m in _gui_items(controller)
        if m.get("action") == "overlay_lease_renew"
    ]
    assert renews == []


def _drive_to_failed_refresh(machine, snapshot_id: str):
    """PAINTED at (sid, gen+1) while the (sid, gen) badges stay on screen.

    Drives painted -> refresh_in_flight (generation bump) -> failed build
    response -> the non-destructive fall-back to PAINTED. The machine pair
    is now one generation AHEAD of the list the user still sees; returns
    the ORIGINAL (sid, gen) -- the pair of the visible list.
    """

    sid, gen = _drive_to_painted(machine, snapshot_id)
    machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    machine.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
            paint_generation=gen + 1, build_ok=False,
        )
    )
    assert machine.state is OverlayState.PAINTED
    assert machine.paint_generation == gen + 1
    return sid, gen


def test_keepalive_renew_carries_visible_pair_after_failed_refresh():
    """The GUI accepts a renew only at the EXACT pair its lease was armed
    with -- the last PRESENTED paint. After a failed refresh the machine
    sits PAINTED one generation ahead while the old badges stay visible,
    so the renew must carry the recorded visible pair, not the machine
    pair (wh-overlay-slow-uia-stale-badges.18.2)."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_failed_refresh(machine, "snap-fr")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, _pin_response)
        controller._overlay_visible_painted_pair = (sid, gen)

        _gui_items(controller).clear()
        controller._fire_overlay_keepalive()
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        return controller, (sid, gen)

    controller, visible = asyncio.run(_run())
    renews = [
        m for m in _gui_items(controller)
        if m.get("action") == "overlay_lease_renew"
    ]
    assert len(renews) == 1
    assert renews[0]["overlay_session_id"] == visible[0]
    assert renews[0]["paint_generation"] == visible[1]


def test_keepalive_renew_carries_machine_pair_when_no_recorded_pair():
    """With no recorded visible pair (None), the renew falls back to the
    machine pair -- the fresh-paint paths keep the two equal."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-fb")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, _pin_response)
        controller._overlay_visible_painted_pair = None

        _gui_items(controller).clear()
        controller._fire_overlay_keepalive()
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        return controller, (sid, gen)

    controller, pair = asyncio.run(_run())
    renews = [
        m for m in _gui_items(controller)
        if m.get("action") == "overlay_lease_renew"
    ]
    assert len(renews) == 1
    assert renews[0]["overlay_session_id"] == pair[0]
    assert renews[0]["paint_generation"] == pair[1]


def test_refresh_failed_paint_ack_closes_clears_and_stops_lease_renew():
    """wh-overlay-slow-uia-stale-badges.18.7: a failed(Q) paint ack at the
    live pair while REFRESH_IN_FLIGHT with no auto-hide is DESTRUCTIVE --
    the manager destroyed the failed monitors' windows (and other monitors
    may already show Q), so the old fall-back to PAINTED let the keepalive
    renew the visible pair and the GUI re-arm its Q lease forever over a
    destroyed or mixed display. The machine must instead close: a clear is
    dispatched at Q, the recorded visible pair is cleared, and the next
    keepalive tick sends NO overlay_lease_renew."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-A")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, _pin_response)
        controller._overlay_visible_painted_pair = (sid, gen)
        controller._overlay_visible_painted_pin = "snap-A"

        machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
        assert machine.state is OverlayState.REFRESH_IN_FLIGHT
        machine.apply(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen + 1, snapshot_id="snap-B",
            )
        )

        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        _gui_items(controller).clear()
        ack = OverlayStateChangedEvent(
            state="failed", overlay_session_id=sid,
            paint_generation=gen + 1, snapshot_id="snap-B",
        ).to_dict()
        await controller._handle_overlay_state_changed(ack)
        await _settle(controller)

        clears = [
            m for m in _gui_items(controller)
            if m.get("action") == "clear_overlay"
        ]
        # (d) the next keepalive tick sends NO overlay_lease_renew.
        _gui_items(controller).clear()
        controller._fire_overlay_keepalive()
        await _settle(controller)
        controller._overlay_cancel_keepalive_timer()
        timer = controller._overlay_pending_clear_timer
        if timer is not None:
            timer.cancel()
        return controller, machine, clears, (sid, gen)

    controller, machine, clears, (sid, gen) = asyncio.run(_run())
    # (a) a clear was dispatched at the refresh pair Q = (sid, gen+1).
    assert len(clears) == 1
    assert clears[0]["overlay_session_id"] == sid
    assert clears[0]["paint_generation"] == gen + 1
    # (b) the machine closed instead of falling back to PAINTED.
    assert machine.state is OverlayState.CLOSED
    # (c) the recorded visible painted pair was cleared with the close.
    assert controller._overlay_visible_painted_pair is None
    # (d) no lease renew left Logic after the close.
    renews = [
        m for m in _gui_items(controller)
        if m.get("action") == "overlay_lease_renew"
    ]
    assert renews == []


def test_expired_ack_in_painted_closes_and_reports_error(caplog):
    """state="expired" at the live pair while PAINTED is the real fault
    case: the GUI tore down badges Logic still believed were on screen.
    The machine closes (no phantom overlay) and the fault is reported as
    an ERROR (popup)."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-ex")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, _pin_response)

        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        ack = OverlayStateChangedEvent(
            state="expired", overlay_session_id=sid, paint_generation=gen,
        ).to_dict()
        await controller._handle_overlay_state_changed(ack)
        await _settle(controller)
        timer = controller._overlay_pending_clear_timer
        if timer is not None:
            timer.cancel()
        return machine

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "expired the badge lease" in errors[0].getMessage()


def test_expired_ack_when_closed_is_warning_bookkeeping(caplog):
    """An expired ack when the machine is not PAINTED at that pair is
    bookkeeping (the lease cleaned up after a clear whose loss the 5s
    watchdog already reported): WARNING, no popup, no state change."""

    async def _run():
        controller = _controller()  # machine starts CLOSED at pair (0, 0)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, _pin_response)

        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        ack = OverlayStateChangedEvent(
            state="expired", overlay_session_id=0, paint_generation=0,
        ).to_dict()
        await controller._handle_overlay_state_changed(ack)
        await _settle(controller)
        return controller.click_overlay_state

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("lease" in r.getMessage() for r in warnings)


def test_expired_ack_at_visible_pair_closes_machine_and_reports_error(caplog):
    """An expired ack at the VISIBLE pair while the machine sits PAINTED
    at a bumped pair (failed-refresh fall-back) is the real fault: the
    display process removed the badges the user could see, and the
    stale gate would otherwise swallow the ack and leave a phantom
    painted overlay (wh-overlay-slow-uia-stale-badges.18.2). Logic must
    log the ERROR, close the machine via a synthetic hide, and mark the
    machine pair as reported so the echo clear cannot double-report."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_failed_refresh(machine, "snap-vx")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, _pin_response)
        controller._overlay_visible_painted_pair = (sid, gen)
        machine_pair = (machine.overlay_session_id, machine.paint_generation)

        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        ack = OverlayStateChangedEvent(
            state="expired", overlay_session_id=sid, paint_generation=gen,
        ).to_dict()
        await controller._handle_overlay_state_changed(ack)
        await _settle(controller)
        timer = controller._overlay_pending_clear_timer
        if timer is not None:
            timer.cancel()
        return controller, machine, machine_pair

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller, machine, machine_pair = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "failed refresh" in errors[0].getMessage()
    assert controller._overlay_clear_fault_reported_pair == machine_pair


def test_echo_clear_deadline_after_expired_fault_logs_warning(caplog):
    """The expired-at-machine-pair ERROR is the report for the incident;
    the defensive echo clear the close dispatches must not raise a
    SECOND ERROR popup when its deadline fires (the GUI crashed right
    after sending the expired ack, so no ack ever arrives). The
    reported-pair downgrade turns the echo's deadline into a WARNING
    (wh-overlay-slow-uia-stale-badges.18.3)."""

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_painted(machine, "snap-ec")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, _pin_response)

        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        ack = OverlayStateChangedEvent(
            state="expired", overlay_session_id=sid, paint_generation=gen,
        ).to_dict()
        await controller._handle_overlay_state_changed(ack)
        await _settle(controller)
        timer = controller._overlay_pending_clear_timer
        if timer is not None:
            timer.cancel()
        return controller, (sid, gen)

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller, pair = asyncio.run(_run())
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1  # the expired report itself
    # The echo clear is pending at the machine pair; fire its deadline as
    # if the GUI is gone and never acks it.
    assert controller._overlay_pending_clear == pair
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller._fire_overlay_clear_ack_deadline(pair[0], pair[1], "tr-ec")
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_send_overlay_startup_reset_puts_reset_action():
    """A starting Logic tells the GUI to drop any leftover badges and start
    a fresh generation gate (a restarted Logic numbers pairs from zero)."""

    controller = _controller()
    controller._send_overlay_startup_reset()
    assert _gui_items(controller) == [{"action": "reset_overlay"}]


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.19: the refresh fall-back is a rollback
# boundary, so the two cross-process sends it races against must be fenced at
# the point of COMMIT -- after the await, not only before it.
#
#   * the build dispatcher must not feed a BUILD_RESPONSE the machine can no
#     longer use. ``_on_painted`` has no build-response cell, so the feed drove
#     the machine to ERROR with the prior badges still on screen, and "click N"
#     then answered numbers_not_showing while the user was looking at numbers.
#   * the paint dispatcher must not present a generation the fall-back already
#     abandoned. The pin ack await postpones the paint, so the screen would
#     show the NEW list while Logic resolves "click N" against the RESTORED
#     prior snapshot.
# ---------------------------------------------------------------------------


def _record_applied_events(controller) -> list:
    """Record every OverlayEvent fed through ``_apply_overlay_event``.

    Wraps the real method rather than replacing it, so the machine still
    advances and the state assertions stay meaningful. ``_feed`` looks the
    attribute up on the instance when it runs, so an instance attribute
    intercepts it.
    """

    applied: list = []
    real = controller._apply_overlay_event

    def _wrapper(event, *, source: str):
        applied.append(event)
        return real(event, source=source)

    controller._apply_overlay_event = _wrapper
    return applied


def test_late_build_response_not_fed_after_fall_back_to_painted():
    """A build reply that lands after the machine left REFRESH_IN_FLIGHT must
    not be fed as a BUILD_RESPONSE.

    The PRE-send staleness check passes here: the machine is PAINTED, not
    CLOSED, and the fall-back kept the SAME pair. Only a post-await fence can
    catch this, and without one the reply reaches ``_on_painted``, which has no
    build-response cell, so the machine fails closed to ERROR.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snapA")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        applied = _record_applied_events(controller)

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            if action == "start_overlay_walk":
                # The refresh deadline expires while Input is still walking:
                # the fall-back commits PAINTED at the same pair and keeps the
                # prior snapshot pinned and visible.
                machine.apply(
                    OverlayEvent(
                        OverlayEventKind.TIMEOUT, overlay_session_id=sid,
                        paint_generation=gen,
                    )
                )
                assert machine.state is OverlayState.PAINTED
                return _walk_response("snapB", sid, gen, 1, 2)
            return _pin_response(action, dict(params or {}))

        controller.app = MagicMock()
        controller.app.send_request = _send_request
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.REFRESH,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr-fence-a")
        await _settle(controller)
        return controller, machine, applied

    controller, machine, applied = asyncio.run(_run())
    fed = [e for e in applied if e.kind is OverlayEventKind.BUILD_RESPONSE]
    assert fed == [], (
        "a late build reply was fed into a machine that had already fallen "
        "back to painted"
    )
    assert machine.state is OverlayState.PAINTED
    assert machine.pinned_snapshot_id == "snapA"
    # Nothing was drawn for the abandoned generation either.
    assert "paint_overlay" not in [
        m.get("action") for m in _gui_items(controller)
    ]


def test_build_response_still_fed_while_the_refresh_is_live():
    """The post-await fence must not drop a reply the machine is waiting for.

    Same shape as the test above with no fall-back: the machine is still
    REFRESH_IN_FLIGHT at the same pair when the reply lands, so the reply is
    exactly what the machine asked for and must be fed.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snapA")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        applied = _record_applied_events(controller)
        _wire_app(
            controller,
            lambda a, p: (
                _walk_response("snapB", sid, gen, 1, 2)
                if a == "start_overlay_walk" else _pin_response(a, p)
            ),
        )
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.REFRESH,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr-fence-d")
        await _settle(controller)
        return controller, machine, applied

    controller, machine, applied = asyncio.run(_run())
    fed = [e for e in applied if e.kind is OverlayEventKind.BUILD_RESPONSE]
    assert len(fed) == 1
    assert fed[0].build_ok is True
    assert fed[0].snapshot_id == "snapB"
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert machine.pinned_snapshot_id == "snapB"


def test_build_response_not_fed_after_the_generation_is_superseded():
    """The post-await fence covers the PAIR as well as the state.

    A supersede that lands while the walk is in flight leaves the machine in
    REFRESH_IN_FLIGHT -- the state half of the fence passes -- at a NEWER
    generation. The reply belongs to the abandoned generation, so the machine
    must never be handed it; leaning on the pre-table generation gate to throw
    it away afterwards is not the same thing.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snapA")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        applied = _record_applied_events(controller)

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            if action == "start_overlay_walk":
                machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
                assert machine.state is OverlayState.REFRESH_IN_FLIGHT
                assert machine.paint_generation == gen + 1
                return _walk_response("snapB", sid, gen, 1, 2)
            return _pin_response(action, dict(params or {}))

        controller.app = MagicMock()
        controller.app.send_request = _send_request
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.REFRESH,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr-fence-e")
        await _settle(controller)
        return machine, applied

    machine, applied = asyncio.run(_run())
    fed = [e for e in applied if e.kind is OverlayEventKind.BUILD_RESPONSE]
    assert fed == [], (
        "a reply for the superseded generation was handed to the machine"
    )
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT


def test_paint_for_a_superseded_pair_is_dropped():
    """The paint fence covers the PAIR as well as the pinned snapshot.

    The paused MIC_RESUME restore repaints the SAME snapshot at a bumped
    generation (wh-overlay-slow-uia-stale-badges.17), so an older paint for
    that snapshot can still be pending when the restore commits. Matching on
    the pinned snapshot alone would let the older one through.
    """

    cache = ClickSnapshotSummaryCache()
    cache.put("snap-p", _summary("snap-p", 1, 2))
    machine = ClickOverlayStateMachine()
    machine.state = OverlayState.PAINTED
    machine.overlay_session_id = 5
    machine.paint_generation = 3
    machine.pinned_snapshot_id = "snap-p"
    controller = _controller(cache=cache, machine=machine)
    _wire_app(controller, lambda a, p: {})

    effect = Effect(
        kind=EffectKind.DISPATCH_PAINT,
        overlay_session_id=5,
        paint_generation=2,
        snapshot_id="snap-p",
    )
    asyncio.run(controller._dispatch_overlay_effects((effect,), trace_id="tr"))
    assert [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ] == []


def test_abandoned_refresh_paint_not_sent_after_fall_back():
    """The pin ack await must not let an abandoned generation reach the GUI.

    The refresh build succeeds, so PIN_SNAPSHOT and DISPATCH_PAINT ship in one
    FIFO-locked batch and the pin ack is awaited FIRST. A slow Input postpones
    that ack past the refresh deadline; the timer fires, the fall-back restores
    the prior pin and commits PAINTED, and the paint that follows would present
    the NEW list while Logic resolves "click N" against the RESTORED prior
    snapshot. The paint must be dropped instead.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snapA")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        pin_seen: list = []

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            params = dict(params or {})
            if action == "start_overlay_walk":
                return _walk_response("snapB", sid, gen, 1, 2)
            if action == "pin_snapshot":
                pin_seen.append(params)
                # The refresh deadline expires while the pin ack is
                # outstanding. Fire the real timer callback so the fall-back
                # runs through the integration exactly as it does in
                # production.
                controller._fire_overlay_timeout(
                    sid, gen, "tr-fence-b",
                    arm_id=controller._overlay_timer_arm_id,
                    armed_state=OverlayState.REFRESH_IN_FLIGHT,
                )
                assert machine.state is OverlayState.PAINTED
                assert machine.pinned_snapshot_id == "snapA"
            return _pin_response(action, params)

        controller.app = MagicMock()
        controller.app.send_request = _send_request
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.REFRESH,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr-fence-b")
        await _settle(controller)
        return controller, machine, pin_seen

    controller, machine, pin_seen = asyncio.run(_run())
    # The build did succeed and the pin for the NEW snapshot did ship, so the
    # race this guards was actually reached (not a setup that stopped early).
    assert [p["snapshot_id"] for p in pin_seen] == ["snapB"]
    assert machine.state is OverlayState.PAINTED
    assert machine.pinned_snapshot_id == "snapA"
    paints = [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ]
    assert paints == [], (
        "the abandoned generation was painted after the fall-back restored "
        "the prior snapshot"
    )


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.21.4: every paint-dispatch path that
# enqueues NO paint must clear the walking cue itself. The GUI clears the cue
# when paint_overlay (or clear_overlay) arrives, so a drop that sends neither
# leaves the moving dot on screen until the GUI fallback timer expires (about
# 6 s by default) -- the dot claims a walk is in flight after the walk ended.
# Two such paths exist: the commit fence and the summary-cache miss.
# ---------------------------------------------------------------------------


def test_walk_cue_cleared_when_the_paint_fence_drops_the_paint():
    """The abandoned-refresh drop must clear the cue.

    Same race as ``test_abandoned_refresh_paint_not_sent_after_fall_back``: the
    fall-back commits PAINTED at the same pair while the paint waits behind the
    pin ack, so the paint is dropped. Nothing else sends a paint_overlay or a
    clear_overlay for that walk, so the drop owns the cue clear.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snapA")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            params = dict(params or {})
            if action == "start_overlay_walk":
                return _walk_response("snapB", sid, gen, 1, 2)
            if action == "pin_snapshot":
                controller._fire_overlay_timeout(
                    sid, gen, "tr-cue-fence",
                    arm_id=controller._overlay_timer_arm_id,
                    armed_state=OverlayState.REFRESH_IN_FLIGHT,
                )
                assert machine.state is OverlayState.PAINTED
            return _pin_response(action, params)

        controller.app = MagicMock()
        controller.app.send_request = _send_request
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.REFRESH,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr-cue-fence")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    # The race was actually reached: the paint was dropped.
    assert machine.state is OverlayState.PAINTED
    assert [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ] == []
    cues = _walk_cue_items(controller)
    assert cues, "no overlay_walk_cue action was enqueued"
    assert cues[0]["active"] is True
    assert cues[-1]["active"] is False, (
        "the paint fence dropped the paint and left the walking cue active"
    )


def test_walk_cue_cleared_when_the_summary_cache_misses():
    """The summary-cache miss must clear the cue too.

    The machine still vouches for the paint (the fence passes), but the summary
    was evicted, so there is nothing to draw and no paint is enqueued. That is
    the same success-without-paint shape as the fence drop.
    """

    machine = ClickOverlayStateMachine()
    machine.state = OverlayState.PAINTED
    machine.overlay_session_id = 7
    machine.paint_generation = 4
    machine.pinned_snapshot_id = "snap-evicted"
    # An EMPTY cache: the summary for the pinned snapshot aged out.
    controller = _controller(cache=ClickSnapshotSummaryCache(), machine=machine)
    _wire_app(controller, lambda a, p: {})

    effect = Effect(
        kind=EffectKind.DISPATCH_PAINT,
        overlay_session_id=7,
        paint_generation=4,
        snapshot_id="snap-evicted",
    )
    asyncio.run(
        controller._dispatch_overlay_effects((effect,), trace_id="tr-cue-miss")
    )

    assert [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ] == []
    cues = _walk_cue_items(controller)
    assert cues, "the cache miss enqueued no overlay_walk_cue action"
    assert cues[-1]["active"] is False, (
        "the summary-cache miss skipped the paint and left the cue active"
    )


def test_walk_cue_not_cleared_when_the_paint_ships():
    """The clear belongs to the DROP paths only.

    A paint the machine still wants ships to the GUI, and the GUI clears the cue
    on receipt. A Logic-side clear here would drop the dot early on the normal
    path, which is what wh-n29v.119.2 forbids.
    """

    cache = ClickSnapshotSummaryCache()
    cache.put("snap-live", _summary("snap-live", 1, 2))
    machine = ClickOverlayStateMachine()
    machine.state = OverlayState.PAINTED
    machine.overlay_session_id = 7
    machine.paint_generation = 4
    machine.pinned_snapshot_id = "snap-live"
    controller = _controller(cache=cache, machine=machine)
    _wire_app(controller, lambda a, p: {})

    effect = Effect(
        kind=EffectKind.DISPATCH_PAINT,
        overlay_session_id=7,
        paint_generation=4,
        snapshot_id="snap-live",
    )
    asyncio.run(
        controller._dispatch_overlay_effects((effect,), trace_id="tr-cue-live")
    )

    assert len([
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ]) == 1
    assert _walk_cue_items(controller) == []


def test_refresh_paint_still_sent_when_the_generation_is_live():
    """The paint fence must not break the ordinary slow-pin success path.

    Same shape as the test above with no timeout: the machine still holds the
    NEW snapshot as its pinned one at the same pair when the pin ack returns,
    so the paint is exactly what the machine wants and must ship.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        sid, gen = _drive_to_refresh_in_flight(machine, "snapA")
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            params = dict(params or {})
            if action == "start_overlay_walk":
                return _walk_response("snapB", sid, gen, 1, 2)
            if action == "pin_snapshot":
                for _ in range(5):
                    await asyncio.sleep(0)
            return _pin_response(action, params)

        controller.app = MagicMock()
        controller.app.send_request = _send_request
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.REFRESH,
        )
        await controller._overlay_dispatch_build(effect, trace_id="tr-fence-c")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert machine.pinned_snapshot_id == "snapB"
    paints = [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ]
    assert len(paints) == 1
    assert paints[0]["overlay_session_id"] == machine.overlay_session_id
    assert paints[0]["paint_generation"] == machine.paint_generation


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.21.3: the paused-resume restore paint is the
# only paint that commits PAINTED with no ack-expecting state behind it, so a
# lost restore paint leaves Logic resolving "click N" against a list the user
# cannot see. The effect carries ``audit_delivery``; the integration audits it.
# ---------------------------------------------------------------------------


class _DeadQueue:
    """A state_to_gui_queue that rejects every put (the Full-queue case)."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_nowait(self, item: dict) -> None:
        raise RuntimeError("queue is full")


def _restored_controller(cache=None, *, snapshot_id="snap-restore"):
    """A controller whose machine just restored from PAUSED to PAINTED.

    The restore paint is returned unscheduled, so the tests below hand it
    straight to ``_dispatch_overlay_effects``. In the running system
    ``_perform_overlay_effects`` schedules it, and that is where the delivery
    entitlement (wh-overlay-slow-uia-stale-badges.21.8) is recorded, so record
    it here to match the state a scheduled restore paint really dispatches in.
    """

    machine = ClickOverlayStateMachine()
    _drive_to_painted(machine, snapshot_id)
    machine.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
    assert machine.state is OverlayState.PAUSED
    restore = machine.apply(
        OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True)
    )
    assert machine.state is OverlayState.PAINTED
    paint = [e for e in restore.effects if e.kind is EffectKind.DISPATCH_PAINT][0]
    if cache is None:
        cache = ClickSnapshotSummaryCache()
        cache.put(snapshot_id, _summary(snapshot_id, 1, 2))
    controller = _controller(cache=cache, machine=machine)
    controller._overlay_restore_paint_entitlement = (
        paint.overlay_session_id, paint.paint_generation,
    )
    _wire_app(controller, lambda a, p: {})
    return controller, machine, paint


def test_restore_paint_dropped_by_the_display_queue_closes_the_overlay():
    """Loss (a): the display queue rejects the paint.

    ``_put_overlay_gui_action`` logs and returns False; nothing retries it. The
    machine has already committed PAINTED, so without the audit Logic keeps
    routing "click N" to badges that were never drawn.
    """

    async def _run():
        controller, machine, paint = _restored_controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller.state_manager.state_to_gui_queue = _DeadQueue()
        await controller._dispatch_overlay_effects((paint,), trace_id="tr-r-a")
        await _settle(controller)
        return machine

    machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED, (
        "an undeliverable restore paint left the machine believing badges "
        "were on screen"
    )


def test_restore_paint_with_no_cached_summary_closes_the_overlay():
    """Loss (b): the summary aged out of the Logic cache.

    ``_overlay_dispatch_paint`` logs a warning and skips the paint. Same end
    state as the queue drop -- nothing was drawn, so the machine must not stay
    PAINTED.
    """

    async def _run():
        controller, machine, paint = _restored_controller(
            cache=ClickSnapshotSummaryCache()  # deliberately empty
        )
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        await controller._dispatch_overlay_effects((paint,), trace_id="tr-r-b")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ] == []
    assert machine.state is OverlayState.CLOSED


def test_restore_paint_never_acked_closes_the_overlay_at_the_deadline():
    """Loss (c): the paint was enqueued but the display never confirmed it.

    Only a deadline can catch this one. The callback is fired directly, the way
    the other timer tests drive ``_fire_overlay_timeout``.
    """

    async def _run():
        controller, machine, paint = _restored_controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        await controller._dispatch_overlay_effects((paint,), trace_id="tr-r-c")
        await _settle(controller)
        assert machine.state is OverlayState.PAINTED  # still waiting
        assert controller._overlay_pending_restore_paint == (
            paint.overlay_session_id, paint.paint_generation,
        )
        controller._fire_overlay_restore_paint_deadline(
            paint.overlay_session_id, paint.paint_generation, "tr-r-c",
        )
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    assert controller._overlay_pending_restore_paint is None


def test_restore_paint_confirmed_by_its_ack_leaves_the_overlay_painted():
    """The success path: the display acks the restore paint.

    The audit resolves, the deadline is cancelled, and the machine stays
    PAINTED. Firing the (now stale) deadline afterwards must change nothing.
    """

    async def _run():
        controller, machine, paint = _restored_controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        await controller._dispatch_overlay_effects((paint,), trace_id="tr-r-d")
        await _settle(controller)
        controller._overlay_note_restore_paint_ack(
            paint.overlay_session_id, paint.paint_generation, "painted",
        )
        assert controller._overlay_pending_restore_paint is None
        controller._fire_overlay_restore_paint_deadline(
            paint.overlay_session_id, paint.paint_generation, "tr-r-d",
        )
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.PAINTED
    paints = [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ]
    assert len(paints) == 1


def test_restore_paint_reported_failed_by_the_display_closes_the_overlay():
    """A "failed" ack is a loss too, and PAINTED has no cell that reacts to it.

    ``OverlayPaintWindowManager.paint`` reports ``failed`` after it has torn its
    windows down, so the badges are NOT on screen. The machine's PAINTED
    paint-ack cell treats that ack as bookkeeping, so the audit is what
    recovers.
    """

    async def _run():
        controller, machine, paint = _restored_controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        await controller._dispatch_overlay_effects((paint,), trace_id="tr-r-e")
        await _settle(controller)
        controller._overlay_note_restore_paint_ack(
            paint.overlay_session_id, paint.paint_generation, "failed",
        )
        await _settle(controller)
        return machine

    machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED


def test_deadline_guarded_paint_arms_no_restore_audit():
    """A walk paint must not arm the audit; its PAINT_IN_FLIGHT deadline owns it."""

    async def _run():
        machine = ClickOverlayStateMachine()
        machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        sid, gen = machine.overlay_session_id, machine.paint_generation
        build = machine.apply(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-walk",
            )
        )
        paint = [
            e for e in build.effects if e.kind is EffectKind.DISPATCH_PAINT
        ][0]
        cache = ClickSnapshotSummaryCache()
        cache.put("snap-walk", _summary("snap-walk", 1, 2))
        controller = _controller(cache=cache, machine=machine)
        _wire_app(controller, lambda a, p: {})
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        await controller._dispatch_overlay_effects((paint,), trace_id="tr-r-f")
        await _settle(controller)
        return controller

    controller = asyncio.run(_run())
    assert controller._overlay_pending_restore_paint is None
    assert controller._overlay_pending_restore_paint_timer is None


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.21.5: the audit must also cover the time the
# restore paint waits for ``_overlay_effect_lock``. ``_apply_overlay_event``
# commits PAINTED synchronously and only SCHEDULES the effects, while an older
# batch holds the lock across every awaited pin and unpin (up to
# ``response_timeout_ms`` each). Arming the audit at enqueue time alone left
# that whole wait unaudited.
# ---------------------------------------------------------------------------


def _paused_by_a_blocked_refresh(controller, sid, gen):
    """Drive the machine to PAUSED with its pin/unpin batch blocked.

    The microphone pauses while a refresh build is in flight; the build reply
    then resolves the refresh straight to PAUSED and schedules PIN(new) +
    UNPIN(prior) + CLEAR. The fake pin send blocks, so that batch holds
    ``_overlay_effect_lock``.
    """

    controller._apply_overlay_event(
        OverlayEvent(OverlayEventKind.MIC_PAUSE), source="test-pause",
    )
    controller._apply_overlay_event(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
            paint_generation=gen, snapshot_id="snap-B",
        ),
        source="test-build",
    )


def _seconds_left(controller):
    """Seconds remaining on the armed restore-paint deadline (None if unarmed)."""

    timer = controller._overlay_pending_restore_paint_timer
    if timer is None:
        return None
    return timer.when() - asyncio.get_running_loop().time()


def _blocked_pin_controller(gate, *, settle_after_click: bool = False):
    """A controller whose pin_snapshot send waits on ``gate``."""

    machine = ClickOverlayStateMachine(settle_after_click=settle_after_click)
    sid, gen = _drive_to_refresh_in_flight(machine, "snap-A")
    cache = ClickSnapshotSummaryCache()
    cache.put("snap-A", _summary("snap-A", 1, 2))
    cache.put("snap-B", _summary("snap-B", 1, 2))
    controller = _controller(cache=cache, machine=machine)
    controller.loop = asyncio.get_running_loop()
    controller.background_tasks = []
    # wh-overlay-slow-uia-stale-badges.2.2.1: nothing in these scenarios moves
    # the foreground, so the window the walk reports reading and the window
    # Logic samples are the SAME one. Pinning both to _identity(1) also keeps
    # the fixture off the real Win32 seam, which the pin point used to reach.
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value=_identity(1)
    )

    async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
        params = dict(params or {})
        if action == "pin_snapshot":
            await gate.wait()
        if action == "start_overlay_walk":
            # wh-overlay-slow-uia-stale-badges.2.1.1: answer a WALK with a walk
            # response. This fixture used to answer every action with a pin
            # response, which reads as a malformed start_overlay_walk payload
            # (no 'outcome' field) and closes the overlay. It went unnoticed
            # while the commit fence dropped every settle reply; now that the
            # fence admits post_click_settling, the reply lands and the wrong
            # payload shape decides the test. The settled read here sees the
            # screen the machine still holds pinned, so it answers "unchanged".
            payload = _walk_response(
                str(params.get("compare_snapshot_id") or "snap-B"),
                int(params.get("overlay_session_id", 0)),
                int(params.get("paint_generation", 0)),
                1, 2,
            )
            payload.update(_read_identity_fields(_identity(1)))
            return payload
        return _pin_response(action, params)

    controller.app = MagicMock()
    controller.app.send_request = _send_request
    return controller, machine, sid, gen


def test_restore_paint_is_audited_while_it_waits_for_the_effect_lock():
    """The window this finding names: PAINTED, no paint queued, no audit.

    The paused-entry batch is blocked on its pin, so the restore paint the
    resume committed to cannot even reach the display queue yet. Logic must
    never be in the painted state with neither a queued restore paint nor an
    active delivery audit -- that is precisely the state in which "click N"
    resolves against badges that are not on the screen.
    """

    async def _run():
        gate = asyncio.Event()
        controller, machine, sid, gen = _blocked_pin_controller(gate)
        _paused_by_a_blocked_refresh(controller, sid, gen)
        for _ in range(6):
            await asyncio.sleep(0)
        assert machine.state is OverlayState.PAUSED

        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True),
            source="test-resume",
        )
        assert machine.state is OverlayState.PAINTED
        pair = (machine.overlay_session_id, machine.paint_generation)
        for _ in range(6):
            await asyncio.sleep(0)

        paints = [
            m for m in _gui_items(controller)
            if m.get("action") == "paint_overlay"
        ]
        blocked = (
            paints,
            controller._overlay_pending_restore_paint,
            _seconds_left(controller),
        )

        gate.set()
        await _settle(controller)
        released = [
            m for m in _gui_items(controller)
            if m.get("action") == "paint_overlay"
        ]
        return controller, machine, pair, blocked, released, _seconds_left(
            controller,
        )

    (
        controller, machine, pair, blocked, released, after_enqueue,
    ) = asyncio.run(_run())
    queued_while_blocked, audited_pair, while_blocked = blocked
    assert queued_while_blocked == [], (
        "the test did not reproduce the wait: the restore paint reached the "
        "display queue instead of blocking behind the pin batch"
    )
    assert machine.state is OverlayState.PAINTED
    assert audited_pair == pair, (
        "the machine was PAINTED with no queued restore paint and no delivery "
        "audit; nothing would have recovered a paint lost in that window"
    )
    display_budget_s = _OVERLAY_RESTORE_ACK_DEADLINE_MS / 1000.0
    assert while_blocked is not None, "the schedule-time audit armed no deadline"
    assert while_blocked > display_budget_s, (
        "the schedule-time deadline carries only the display budget, so a "
        "healthy pin batch that outlasts it would close a working overlay"
    )
    # Once the lock is free the paint ships and the audit is still the one for
    # this pair, RE-ARMED so the display leg measures its own budget from the
    # enqueue rather than sharing the schedule-time one.
    assert len(released) == 1
    assert released[0]["paint_generation"] == pair[1]
    assert controller._overlay_pending_restore_paint == pair
    assert after_enqueue is not None
    assert after_enqueue <= display_budget_s, (
        "the enqueue did not re-arm the deadline; the display leg is sharing "
        "the schedule-time budget"
    )


def test_schedule_time_restore_audit_budget_covers_the_lock_wait():
    """The schedule-time budget is the lock wait PLUS the display budget.

    Measuring the plain display budget from the machine's commit would fire
    the audit during a HEALTHY paused-entry batch: that batch awaits a pin and
    an unpin, each bounded by ``response_timeout_ms``, which at the shipped
    default of 3000ms already outlasts the display budget on its own. The
    audit would then close a working overlay and pop an error at the user.
    """

    controller = _controller()
    controller.click_config.response_timeout_ms = 3000
    assert controller._overlay_restore_paint_schedule_budget_ms() == (
        _OVERLAY_RESTORE_ACK_DEADLINE_MS + 2 * 3000
    )
    # An operator who raises the request timeout raises the allowance with it,
    # so a slow hardware tier does not turn the audit into a false alarm.
    controller.click_config.response_timeout_ms = 9000
    assert controller._overlay_restore_paint_schedule_budget_ms() == (
        _OVERLAY_RESTORE_ACK_DEADLINE_MS + 2 * 9000
    )


# The schedule-time budget doubles ``response_timeout_ms``. Until
# wh-click-response-timeout-unbounded, ui/click_config.py validated that key
# with ``_is_int_at_least(100)`` and tomllib parsed arbitrary-precision
# integers rather than enforcing TOML's 64-bit range, so a TOML-valid value of
# 10**308 reached a live, ENABLED ClickConfig. The validator now bounds the key
# at 10000, but these tests set the field on the config object directly, which
# is the path that still bypasses the validator. The next two tests are the
# bound that keeps the derived delay representable
# (wh-overlay-slow-uia-stale-badges.21.10).
_HUGE_TIMEOUT_MS = 10 ** 308


def test_schedule_time_restore_audit_budget_stays_a_representable_delay():
    """A config value float cannot hold must not reach ``call_later``.

    ``budget_ms / 1000.0`` raises OverflowError when the budget exceeds what a
    float can represent, and it raises INSIDE the arm -- before the dispatch
    task exists, with the machine already committed PAINTED. The ceiling is far
    above any timeout an operator would really write, so no legitimate
    configuration is clamped and the audit can never fire early on a slow
    machine.
    """

    controller = _controller()
    controller.click_config.response_timeout_ms = _HUGE_TIMEOUT_MS
    budget_ms = controller._overlay_restore_paint_schedule_budget_ms()
    assert budget_ms == _OVERLAY_RESTORE_SCHEDULE_BUDGET_MAX_MS
    # The assertion that matters: the derived delay converts to a float.
    assert budget_ms / 1000.0 > _OVERLAY_RESTORE_ACK_DEADLINE_MS / 1000.0
    # A value below the ceiling is still reported exactly, not clamped.
    controller.click_config.response_timeout_ms = 9000
    assert controller._overlay_restore_paint_schedule_budget_ms() == (
        _OVERLAY_RESTORE_ACK_DEADLINE_MS + 2 * 9000
    )


def test_a_huge_response_timeout_still_paints_and_audits_the_restore():
    """The whole resume path survives the largest TOML-valid timeout.

    The unit test above bounds the number; this one proves the consequence the
    finding names. Without the bound the arm raises out of
    ``_perform_overlay_effects`` before the dispatch task is created, so the
    machine sits PAINTED with no restore paint and no audit -- routing keeps
    resolving 'click N' against badges that never reached the screen.
    """

    async def _run():
        gate = asyncio.Event()
        controller, machine, sid, gen = _blocked_pin_controller(gate)
        controller.click_config.response_timeout_ms = _HUGE_TIMEOUT_MS
        _paused_by_a_blocked_refresh(controller, sid, gen)
        for _ in range(6):
            await asyncio.sleep(0)
        assert machine.state is OverlayState.PAUSED

        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True),
            source="test-resume",
        )
        assert machine.state is OverlayState.PAINTED
        pair = (machine.overlay_session_id, machine.paint_generation)
        audited = controller._overlay_pending_restore_paint
        gate.set()
        await _settle(controller)
        painted = [
            m for m in _gui_items(controller)
            if m.get("action") == "paint_overlay"
        ]
        return pair, audited, painted

    pair, audited, painted = asyncio.run(_run())
    assert audited == pair, (
        "the schedule-time arm raised before it recorded the audit, so the "
        "machine committed PAINTED with nothing watching the restore"
    )
    assert len(painted) == 1 and painted[0]["paint_generation"] == pair[1], (
        "the restore paint never reached the display queue"
    )


def test_restore_paint_the_machine_abandons_while_blocked_drops_its_audit():
    """A superseded restore paint must not leave an audit that fires an error.

    Same blocked batch, but a hide moves the machine on before the paint can
    reach the display queue. The commit fence drops the paint, and the audit
    has to go with it: the machine already abandoned the paint, so there is no
    false belief to correct.
    """

    async def _run():
        gate = asyncio.Event()
        controller, machine, sid, gen = _blocked_pin_controller(gate)
        _paused_by_a_blocked_refresh(controller, sid, gen)
        for _ in range(6):
            await asyncio.sleep(0)
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True),
            source="test-resume",
        )
        for _ in range(6):
            await asyncio.sleep(0)
        assert controller._overlay_pending_restore_paint is not None
        # 'hide numbers' while the paint is still behind the lock.
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.HIDE_NUMBERS), source="test-hide",
        )
        gate.set()
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    assert [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ] == []
    assert controller._overlay_pending_restore_paint is None
    assert controller._overlay_pending_restore_paint_timer is None


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.21.8: an exit that keeps BOTH the pair and
# the pin supersedes an audited restore paint that is still waiting for
# ``_overlay_effect_lock``. The ordinary paint fence deliberately reads only
# the pair and the pin, so it passes that superseded paint and the user sees a
# frame of badges the machine no longer wants. The schedule-time audit is left
# armed as well, and it reports a user-visible loss for an overlay the machine
# has already taken off the screen.
# ---------------------------------------------------------------------------


async def _restore_paint_blocked_behind_the_pin(controller, machine, sid, gen):
    """Resume to PAINTED while the paused-entry batch still holds the lock.

    Returns the (session, generation) pair of the audited restore paint, which
    is still queued behind ``_overlay_effect_lock`` when this returns.
    """

    _paused_by_a_blocked_refresh(controller, sid, gen)
    for _ in range(6):
        await asyncio.sleep(0)
    assert machine.state is OverlayState.PAUSED
    controller._apply_overlay_event(
        OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True),
        source="test-resume",
    )
    assert machine.state is OverlayState.PAINTED
    pair = (machine.overlay_session_id, machine.paint_generation)
    for _ in range(6):
        await asyncio.sleep(0)
    assert [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ] == [], (
        "the test did not reproduce the wait: the restore paint reached the "
        "display queue instead of blocking behind the pin batch"
    )
    assert controller._overlay_pending_restore_paint == pair
    return pair


def _restore_loss_errors(caplog):
    """Every user-visible restore-paint loss report in ``caplog``."""

    return [
        r for r in caplog.records
        if "restored numbered badges" in r.getMessage()
        and r.levelno >= logging.ERROR
    ]


def test_second_pause_while_blocked_drops_the_restore_paint_and_its_audit(
    caplog,
):
    """A second mic pause supersedes the restore paint at the SAME pair.

    ``_on_painted`` MIC_PAUSE returns the machine to PAUSED without bumping the
    generation and without touching the pin, so the pair-and-pin fence still
    reads the queued restore paint as current. Delivering it puts badges back
    on the screen while the microphone is paused, and the pause's own clear
    only follows in a later batch.
    """

    async def _run():
        gate = asyncio.Event()
        controller, machine, sid, gen = _blocked_pin_controller(gate)
        pair = await _restore_paint_blocked_behind_the_pin(
            controller, machine, sid, gen,
        )
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_PAUSE), source="test-pause-2",
        )
        assert machine.state is OverlayState.PAUSED
        assert (machine.overlay_session_id, machine.paint_generation) == pair, (
            "the second pause bumped the generation, so the ordinary fence "
            "would have caught the paint and this exit is not the one the "
            "finding names"
        )
        assert machine.pinned_snapshot_id == "snap-B", (
            "the second pause dropped the pin, so the ordinary fence would "
            "have caught the paint"
        )
        # The audit must be retired HERE, while the lock is still held -- not
        # when the abandoned batch finally runs.
        retired_while_blocked = (
            controller._overlay_pending_restore_paint,
            controller._overlay_pending_restore_paint_timer,
        )
        gate.set()
        await _settle(controller)
        # The deadline the resume armed is the one that would report the loss.
        controller._fire_overlay_restore_paint_deadline(
            pair[0], pair[1], "tr-21-8-pause",
        )
        await _settle(controller)
        return controller, machine, pair, retired_while_blocked

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller, machine, pair, retired = asyncio.run(_run())

    assert [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ] == [], (
        "the superseded restore paint was delivered, so the badges came back "
        "on screen while the microphone was paused"
    )
    assert retired == (None, None), (
        "the second pause left the restore audit armed; a dropped painted "
        "report would then report a lost restore for an overlay the pause "
        "had already taken off the screen"
    )
    assert machine.state is OverlayState.PAUSED
    assert controller._overlay_pending_restore_paint is None
    assert controller._overlay_pending_restore_paint_timer is None
    assert _restore_loss_errors(caplog) == [], (
        "the stale deadline reported a user-visible lost restore for a paint "
        "the machine itself had abandoned"
    )


def test_post_click_settling_while_blocked_drops_the_restore_paint_and_its_audit(
    caplog,
):
    """The settling entry is the other same-pair, same-pin exit.

    ``_enter_post_click_settling`` deliberately keeps the pin, and the clear it
    dispatches has to carry the generation the GUI painted, so the queued
    restore paint reaches the ordinary fence holding the SAME pin -- and would
    repaint badges into the gap the settling state exists to leave empty.

    Child .2 bumps the generation after that clear, for the settle build, so
    the queued restore is now stale by generation as well. That makes the
    fence a second line of defence, not the first: what this test proves is
    still the controller's own retirement at the settling entry, asserted
    below as ``retired == (None, None)`` -- controller state read immediately
    after the entry and before the blocked pin is released, which no fence
    can produce.
    """

    async def _run():
        gate = asyncio.Event()
        controller, machine, sid, gen = _blocked_pin_controller(
            gate, settle_after_click=True,
        )
        pair = await _restore_paint_blocked_behind_the_pin(
            controller, machine, sid, gen,
        )
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.CLICK_COMPLETE),
            source="test-click-complete",
        )
        assert machine.state is OverlayState.POST_CLICK_SETTLING
        # Same session, one generation on: the settle build's pair.
        assert machine.overlay_session_id == pair[0]
        assert machine.paint_generation == pair[1] + 1
        assert machine.pinned_snapshot_id == "snap-B"
        retired_while_blocked = (
            controller._overlay_pending_restore_paint,
            controller._overlay_pending_restore_paint_timer,
        )
        gate.set()
        await _settle(controller)
        controller._fire_overlay_restore_paint_deadline(
            pair[0], pair[1], "tr-21-8-settle",
        )
        await _settle(controller)
        return controller, machine, retired_while_blocked, pair

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller, machine, retired, pair = asyncio.run(_run())

    # wh-overlay-slow-uia-stale-badges.2.1.1: the forbidden paint is named by
    # its PAIR, not by "any paint at all". The settle build the settling entry
    # dispatched now reaches the machine, and its unchanged answer repaints the
    # held pin at the NEXT generation -- that repaint is what ends the gap and
    # is the whole point of this bead. The superseded restore paint carries the
    # generation the GUI had already painted, and that one must never arrive.
    assert [
        m for m in _gui_items(controller)
        if m.get("action") == "paint_overlay"
        and (m.get("overlay_session_id"), m.get("paint_generation")) == pair
    ] == [], (
        "the superseded restore paint was delivered into the post-click "
        "settling gap, which exists to hold no badges at all"
    )
    assert retired == (None, None), (
        "the settling entry left the restore audit armed"
    )
    # The settle reply landed and the machine left the gap by repainting the
    # held pin. Asserting POST_CLICK_SETTLING here would assert the defect
    # wh-overlay-slow-uia-stale-badges.2.1.1 named: a settle reply dropped at
    # the commit fence, leaving the state to expire on its own timer.
    assert machine.state is OverlayState.PAINT_IN_FLIGHT
    assert machine.pinned_snapshot_id == "snap-B"
    assert controller._overlay_pending_restore_paint is None
    assert controller._overlay_pending_restore_paint_timer is None
    assert _restore_loss_errors(caplog) == []


def test_hide_command_while_blocked_retires_the_restore_audit(caplog):
    """'hide numbers' reaches the machine by the OTHER apply path.

    ``handle_overlay_command`` applies HIDE_NUMBERS itself and never runs
    ``_apply_overlay_event``, so it has to retire the audited restore's
    entitlement and its audit on its own, like every sibling reconcile.
    """

    async def _run():
        gate = asyncio.Event()
        controller, machine, sid, gen = _blocked_pin_controller(gate)
        pair = await _restore_paint_blocked_behind_the_pin(
            controller, machine, sid, gen,
        )
        await controller.handle_overlay_command("hide", "tr-21-8-hide-cmd")
        assert machine.state is OverlayState.CLOSED
        retired_while_blocked = (
            controller._overlay_pending_restore_paint,
            controller._overlay_pending_restore_paint_timer,
            controller._overlay_restore_paint_entitlement,
        )
        controller._fire_overlay_restore_paint_deadline(
            pair[0], pair[1], "tr-21-8-hide-cmd",
        )
        gate.set()
        await _settle(controller)
        return controller, retired_while_blocked

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller, retired = asyncio.run(_run())

    assert retired == (None, None, None), (
        "the hide command left the restore audit and its delivery entitlement "
        "in place"
    )
    assert [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ] == []
    assert _restore_loss_errors(caplog) == []


@pytest.mark.parametrize(
    "kind",
    [
        OverlayEventKind.HIDE_NUMBERS,
        OverlayEventKind.SHOW_NUMBERS,
        OverlayEventKind.FOCUS_CHANGE,
    ],
)
def test_exit_while_blocked_retires_the_restore_audit_at_once(kind, caplog):
    """The exits the ordinary fence DOES catch still strand the audit.

    A hide, a re-said 'show numbers' and a focus change each move the pair or
    the pin, so the queued restore paint is dropped when the lock finally
    frees. Until then the schedule-time audit is still armed, and its deadline
    can fire inside that wait: the user gets a lost-restore error for an
    overlay the machine has already taken off the screen, and the recovery
    behind it then does nothing. The audit has to go when the machine leaves
    the painted state, not when the abandoned batch runs.
    """

    async def _run():
        gate = asyncio.Event()
        controller, machine, sid, gen = _blocked_pin_controller(gate)
        pair = await _restore_paint_blocked_behind_the_pin(
            controller, machine, sid, gen,
        )
        controller._apply_overlay_event(
            OverlayEvent(kind), source=f"test-{kind.value}",
        )
        assert machine.state is not OverlayState.PAINTED
        retired_while_blocked = (
            controller._overlay_pending_restore_paint,
            controller._overlay_pending_restore_paint_timer,
        )
        # The deadline armed at schedule time is still live in the real
        # system; fire it here the way the loop would have.
        controller._fire_overlay_restore_paint_deadline(
            pair[0], pair[1], f"tr-21-8-{kind.value}",
        )
        gate.set()
        await _settle(controller)
        return controller, retired_while_blocked

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller, retired = asyncio.run(_run())

    assert retired == (None, None), (
        "the exit left the schedule-time restore audit armed while the paint "
        "was still waiting for the effect lock"
    )
    assert _restore_loss_errors(caplog) == [], (
        "the stranded audit reported a user-visible lost restore for a paint "
        "the machine had already abandoned"
    )


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.21.6: ``_recover_lost_restore_paint`` marks
# the pair as already-reported BEFORE it defers the synthetic hide. When the
# deferred half re-validates and does nothing, that marker outlives a clear
# that never existed, and it then downgrades a later genuine clear fault at the
# same pair from an ERROR the user sees to a WARNING only the log holds.
# ---------------------------------------------------------------------------


def test_recovery_that_does_nothing_leaves_a_later_clear_fault_an_error(caplog):
    """A same-pair pause wins the race, so no synthetic clear was ever sent.

    The genuine clear the pause dispatches is then rejected by the display
    queue. That is a first fault at this pair, so it must reach the user as an
    ERROR; the recovery it raced never sent a clear to deduplicate against.
    """

    async def _run():
        controller, machine, paint = _restored_controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        sid = paint.overlay_session_id
        gen = paint.paint_generation
        await controller._dispatch_overlay_effects((paint,), trace_id="tr-race")
        await _settle(controller)
        assert machine.state is OverlayState.PAINTED

        # The restore paint is never acked, so its deadline fires and queues
        # the synthetic hide with call_soon.
        controller._fire_overlay_restore_paint_deadline(sid, gen, "tr-race")
        # A real mic pause wins the race, BEFORE that queued callback runs. The
        # pause clears at the very same pair, and the display queue rejects it.
        controller.state_manager.state_to_gui_queue = _DeadQueue()
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_PAUSE), source="test-pause",
        )
        await _settle(controller)
        return controller, machine, (sid, gen)

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller, machine, pair = asyncio.run(_run())

    assert machine.state is OverlayState.PAUSED, (
        "the pause did not win the race, so the no-op recovery leg was not "
        "exercised"
    )
    faults = [
        r for r in caplog.records
        if "could not be handed to the display process" in r.getMessage()
    ]
    assert len(faults) == 1
    assert faults[0].levelno == logging.ERROR, (
        "a genuine lost clear was downgraded to a WARNING by a marker the "
        "recovery left behind without ever sending a clear"
    )


def test_recovery_that_sends_its_clear_still_dedupes_the_echo(caplog):
    """The marker must still suppress the second notice for ONE incident.

    Nothing wins the race here, so the deferred half does send the synthetic
    hide. The clear that hide dispatches carries the audited pair, and the
    display queue rejects it: that IS the recovery's own clear, so it stays a
    WARNING rather than popping a second error for one incident.
    """

    async def _run():
        controller, machine, paint = _restored_controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        sid = paint.overlay_session_id
        gen = paint.paint_generation
        await controller._dispatch_overlay_effects((paint,), trace_id="tr-echo")
        await _settle(controller)
        controller.state_manager.state_to_gui_queue = _DeadQueue()
        controller._fire_overlay_restore_paint_deadline(sid, gen, "tr-echo")
        await _settle(controller)
        return controller, machine, (sid, gen)

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.main"):
        controller, machine, pair = asyncio.run(_run())

    assert machine.state is OverlayState.CLOSED
    assert controller._overlay_clear_fault_reported_pair == pair
    faults = [
        r for r in caplog.records
        if "could not be handed to the display process" in r.getMessage()
    ]
    assert len(faults) == 1
    assert faults[0].levelno == logging.WARNING, (
        "the recovery's own clear popped a second error for one incident"
    )


def _machine_in_post_click_settling(snapshot_id: str = "snap-pre"):
    """A settle-enabled machine sitting in post_click_settling, as after a click.

    closed -> walk_in_flight -> paint_in_flight -> painted -> post_click_settling,
    the only sequence that reaches the state in production.
    """
    machine = ClickOverlayStateMachine(settle_after_click=True)
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sess, gen = machine.overlay_session_id, machine.paint_generation
    machine.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE,
            overlay_session_id=sess,
            paint_generation=gen,
            snapshot_id=snapshot_id,
        )
    )
    machine.apply(
        OverlayEvent(
            OverlayEventKind.PAINT_ACK,
            overlay_session_id=sess,
            paint_generation=gen,
            paint_state=PaintAckState.PAINTED,
        )
    )
    assert machine.state is OverlayState.PAINTED
    machine.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
    assert machine.state is OverlayState.POST_CLICK_SETTLING
    return machine


def _read_identity_fields(identity) -> dict:
    """The four identity keys a walk reply carries for the window it READ.

    ``StartOverlayWalkResponse`` mirrors the ``ForegroundContext`` names
    (``foreground_window`` / ``foreground_pid`` / ``foreground_process_name`` /
    ``foreground_window_creation_time``); ``ForegroundIdentity`` uses the short
    names. This maps one onto the other so a test can state the window in one
    vocabulary (``_identity(n)``) and put it on the wire in the other.
    """

    return {
        "foreground_window": identity.hwnd,
        "foreground_pid": identity.pid,
        "foreground_process_name": identity.process_name,
        "foreground_window_creation_time": identity.window_creation_time,
    }


def _run_settle_build(
    machine,
    walk_snapshot_id: str,
    *,
    read_identity=None,
    current_identity=None,
):
    """Dispatch the SETTLE build through the real controller and let it finish.

    ``read_identity`` is the window the Input side reports it ACTUALLY READ; it
    rides the walk reply. ``current_identity`` is what the Logic side samples as
    the CURRENT foreground. They are the same window by default -- the ordinary
    case, where nothing moved while the window settled -- so a caller only names
    them when it wants the foreground to have moved
    (wh-overlay-slow-uia-stale-badges.2.2.1).
    """

    sid, gen = machine.overlay_session_id, machine.paint_generation
    read = _identity(1) if read_identity is None else read_identity
    current = _identity(1) if current_identity is None else current_identity

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
            return_value=current
        )

        def _respond(action, params):
            if action == "start_overlay_walk":
                payload = _walk_response(walk_snapshot_id, sid, gen, 1, 2)
                payload.update(_read_identity_fields(read))
                return payload
            return _pin_response(action, params)

        _wire_app(controller, _respond)
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SETTLE,
            snapshot_id="snap-pre",
        )
        await controller._dispatch_overlay_effects((effect,), trace_id="tr")
        await _settle(controller)
        return controller

    return asyncio.run(_run())


def test_the_settle_reply_reaches_the_machine_that_is_still_settling():
    """The commit fence must admit post_click_settling, or the badges never return.

    ``_overlay_dispatch_build._feed`` re-checks the machine at the commit point
    and drops a reply the machine can no longer use
    (wh-overlay-slow-uia-stale-badges.19). Its list of states that CAN still use
    a reply was written when only walk_in_flight and refresh_in_flight had a
    BUILD_RESPONSE cell that does work. This bead added a third:
    ``_on_post_click_settling`` repaints the held pin or paints the new
    snapshot. The settle build is dispatched FROM post_click_settling and the
    machine is still there when the reply lands, at the SAME
    (overlay_session_id, paint_generation) pair -- so a fence that omits the
    state drops every settle reply, and the state closes on its own timer with
    no repaint. That is the whole feature failing, and it is invisible to a
    machine-level test, which never runs the fence.

    Found by deepseek round 1 as wh-overlay-slow-uia-stale-badges.2.1.1.
    """
    machine = _machine_in_post_click_settling()

    controller = _run_settle_build(machine, "snap-pre")

    walks = [
        params for action, params in controller._sent  # type: ignore[attr-defined]
        if action == "start_overlay_walk"
    ]
    assert len(walks) == 1, "the settle build must still reach Input"
    # The reply was CONSUMED: the unchanged answer repaints the held pin, which
    # is the paint_in_flight transition. A dropped reply leaves the machine
    # sitting in post_click_settling until its timer fires.
    assert machine.state is OverlayState.PAINT_IN_FLIGHT
    assert machine.pinned_snapshot_id == "snap-pre"


def test_a_changed_settle_reply_also_reaches_the_still_settling_machine():
    """The other leg of the same fence: a DIFFERENT snapshot id must land too.

    The unchanged leg and the changed leg arrive through the identical fence,
    so a fix that admitted only one of them would be no fix at all. Here the
    settled read answers with a new snapshot, and the machine must unpin the
    pre-click snapshot, pin the new one and paint it.
    """
    machine = _machine_in_post_click_settling()

    controller = _run_settle_build(machine, "snap-after")

    assert len(controller._sent) >= 1  # type: ignore[attr-defined]
    assert machine.state is OverlayState.PAINT_IN_FLIGHT
    assert machine.pinned_snapshot_id == "snap-after"


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.2.2.1: the settle reply describes ONE
# window, and the foreground can move while the window settles. The commit
# fence revalidates the reply's window against the current foreground before
# the machine can pin or paint it.
# ---------------------------------------------------------------------------


def test_a_settle_reply_for_another_window_closes_instead_of_painting():
    """The badges read from window A must never be painted over window B.

    ``post_click_settling``'s FOCUS_CHANGE cell is a deliberate NO_OP: a click
    that opens a dialog CHANGES the foreground, and that is the common case
    for 'click N', so the machine must not cancel on it. The cell's comment
    delegates the safety to the integration -- "the integration picks the
    window to read and revalidates its identity before it paints". This is
    that revalidation.

    Input reads window 1 and reports it. By the time the reply reaches the
    commit fence the foreground is window 2, so the numbers describe controls
    that are no longer on screen. The reply must NOT be painted; the overlay
    fails closed, exactly as the state's own TIMEOUT arm does, and the user
    says 'show numbers' again.
    """
    machine = _machine_in_post_click_settling()

    controller = _run_settle_build(
        machine, "snap-after",
        read_identity=_identity(1),
        current_identity=_identity(2),
    )

    walks = [
        params for action, params in controller._sent  # type: ignore[attr-defined]
        if action == "start_overlay_walk"
    ]
    assert len(walks) == 1, "the settle build still reaches Input"
    paints = [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ]
    assert paints == [], (
        "a reply describing window 1 was painted while window 2 is foreground"
    )
    assert machine.state is OverlayState.CLOSED, (
        "the overlay must fail closed on a foreground-identity mismatch"
    )
    assert machine.pinned_snapshot_id is None, (
        "failing closed must not strand the pre-click pin"
    )


def test_a_settle_reply_for_the_same_window_still_paints():
    """The other half of the rule: an unmoved foreground still repaints.

    A check that dropped every settle reply would 'fix' the stale paint by
    removing the feature. This is the ordinary case -- the click did not move
    the foreground -- and it must reach paint exactly as before.
    """
    machine = _machine_in_post_click_settling()

    controller = _run_settle_build(
        machine, "snap-after",
        read_identity=_identity(3),
        current_identity=_identity(3),
    )

    paints = [
        m for m in _gui_items(controller) if m.get("action") == "paint_overlay"
    ]
    assert len(paints) == 1
    assert paints[0]["snapshot_id"] == "snap-after"
    assert machine.state is OverlayState.PAINT_IN_FLIGHT
    assert machine.pinned_snapshot_id == "snap-after"


def test_settle_pin_brands_the_snapshot_with_the_window_the_read_reported():
    """The pin must carry the READ's window, not a fresh sample.

    ``_overlay_send_pin`` used to call ``_capture_overlay_foreground_identity``
    at the pin instant, so a snapshot read from window 1 was branded with
    whatever was foreground when the pin went out. That defeats the two
    consumers of the recorded identity -- ``overlay_snapshot_is_valid_on_resume``
    and ``_overlay_refresh_visible_window_is_foreground`` -- by the SAME race
    the fence closes: both then compare window 2 against window 2 and pass.

    The pin point is asserted directly rather than through a whole settle run
    because the two samples cannot be told apart end to end: the fixed code
    samples once at the fence and the broken code samples once at the pin, so
    a call-ordered fake returns the same value to both. The slot the fence
    fills is therefore set here, and the fresh sample is made to disagree with
    it.
    """
    controller = _controller()
    _wire_app(controller, lambda a, p: _pin_response(a, p))
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value=_identity(2)
    )
    controller._overlay_settle_read_identity = ("snap-after", _identity(1))
    effect = Effect(
        kind=EffectKind.PIN_SNAPSHOT, overlay_session_id=7,
        paint_generation=0, snapshot_id="snap-after",
    )

    asyncio.run(controller._dispatch_overlay_effects((effect,), trace_id="tr"))

    assert (controller._overlay_snapshot_window_identity["snap-after"]
            == _identity(1))
    assert controller._overlay_tracked_identity == _identity(1)
    assert controller._overlay_settle_read_identity is None, (
        "the carried identity is consumed by the pin it was recorded for"
    )


def test_settle_deadline_bounds_the_state_even_when_the_read_hangs():
    """The settling deadline must bound the STATE, not start after the read.

    wh-overlay-slow-uia-stale-badges.2.2.3 (codex round 4).
    ``_enter_post_click_settling`` used to emit DISPATCH_BUILD before
    ARM_TIMER, and ``_dispatch_overlay_effects`` awaits one batch in order
    under a single lock. So the settling timer was armed only AFTER the
    settle read's ``send_request`` returned, and ``settle_deadline_ms`` did
    not bound the time spent in POST_CLICK_SETTLING at all -- the real bound
    was ``[click] response_timeout_ms``, which ``ui/click_config.py``
    validates with a minimum of 100 and no ceiling. With a
    ``response_timeout_ms`` above ``settle_deadline_ms`` and an Input process
    that does not answer, the machine sat in POST_CLICK_SETTLING past its own
    deadline, kept the pre-click pin, and refused every spoken number as
    numbers_updating.

    This test blocks ``start_overlay_walk`` far longer than the settling
    deadline and asserts the machine has already left POST_CLICK_SETTLING
    without the settle send ever answering. ``send_request`` is a mock
    here, so every claim below is about Logic; see
    wh-overlay-slow-uia-stale-badges.11 for the Input-process half.

    The "not for the wrong reason" guard used to be "the effect batch had not
    finished yet". wh-overlay-slow-uia-stale-badges.2.2.4 made that
    unobservable: the deadline now WAKES the parked settle send, so the batch
    finishes within milliseconds of the state change. The guard is stronger
    now instead of weaker -- it asserts the settle send never produced an
    answer at all, so the state cannot have left POST_CLICK_SETTLING because
    the send completed.
    """

    machine = ClickOverlayStateMachine(
        settle_after_click=True, settle_deadline_ms=30,
    )
    _drive_to_painted(machine, "snap")

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        # Far above the settling deadline, which is exactly the configuration
        # the finding names. A valid ClickConfig allows it.
        controller.click_config.response_timeout_ms = 5000
        controller.click_config.screen_read_timeout_ms = 5000

        settle_read_answered = False

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            nonlocal settle_read_answered
            if action == "start_overlay_walk":
                # The mock never answers within the settling deadline.
                await asyncio.sleep(0.40)
                settle_read_answered = True
            return {}

        controller.app = MagicMock()
        controller.app.send_request = _send_request

        result = machine.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
        assert machine.state is OverlayState.POST_CLICK_SETTLING

        batch = asyncio.ensure_future(
            controller._dispatch_overlay_effects(result.effects, trace_id="tr")
        )
        # Past the 30 ms settling deadline, and far short of the 400 ms send.
        await asyncio.sleep(0.15)
        state_at_the_deadline = machine.state
        answered_by_now = settle_read_answered
        await batch
        await _settle(controller)
        return state_at_the_deadline, answered_by_now

    state_at_deadline, read_answered = asyncio.run(_run())

    assert not read_answered, (
        "the settle send must never have answered; otherwise the machine "
        "could have left post_click_settling because the read finished "
        "rather than because settle_deadline_ms expired"
    )
    assert state_at_deadline is OverlayState.CLOSED, (
        "settle_deadline_ms must end POST_CLICK_SETTLING on its own; the "
        f"machine was still {state_at_deadline.value} while the settle send "
        "was outstanding, so the real bound was response_timeout_ms"
    )


def test_a_timed_out_settle_does_not_park_the_next_show_numbers():
    """A settle that timed out must not hold the next walk behind its dead send.

    wh-overlay-slow-uia-stale-badges.2.2.4 (codex round 5). Commit 871a52a7
    arms the settling timer BEFORE the settle DISPATCH_BUILD, so for the first
    time the TIMEOUT can fire while that build is still parked on its Input
    round trip holding ``_overlay_effect_lock``. The TIMEOUT closes the machine
    synchronously, but nothing woke the parked build:
    ``_abort_inflight_overlay_build`` was reachable only from
    ``handle_overlay_command``'s HIDE_NUMBERS arm. A "show numbers" spoken
    right after the deadline therefore queued its own DISPATCH_BUILD behind the
    dead settle send and did not start until ``response_timeout_ms`` expired --
    a key ``ui/click_config.py`` validates with a minimum of 100 and no
    ceiling. The user was left with no badges and no running deadline.

    This test stalls the settle send indefinitely, waits past the settling
    deadline, says "show numbers", and asserts Logic issued a second
    ``start_overlay_walk`` while the first send was still unanswered.

    What this proves, and where the proof stops
    (wh-overlay-slow-uia-stale-badges.2.2.5, codex round 6). ``send_request``
    is a mock here, so this is a LOGIC-side proof: it shows Logic issues the
    second ``start_overlay_walk`` request instead of parking behind the dead
    one. It does not exercise ``app.py`` delivery or the Input process's single
    synchronous command loop. Once a frame reaches shared memory nothing
    withdraws it -- ``app.py`` checks ``_delivery_abandoned`` before the write
    and states outright that it cannot change after -- so an Input process
    already inside a hung UIA read stays there. That containment is
    wh-overlay-slow-uia-stale-badges.11 ("Contain the single command loop in
    the Input process: bounded queue, stale-command expiry, cancellation"), an
    open sibling bead, and it is deliberately not this branch's work.
    """

    machine = ClickOverlayStateMachine(
        settle_after_click=True, settle_deadline_ms=30,
    )
    _drive_to_painted(machine, "snap")

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        # Far above the settling deadline. A valid ClickConfig allows it.
        controller.click_config.response_timeout_ms = 5000
        controller.click_config.screen_read_timeout_ms = 5000

        walks: list[object] = []
        release = asyncio.Event()
        first_send_returned = False

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            nonlocal first_send_returned
            if action == "start_overlay_walk":
                walks.append(params)
                if len(walks) == 1:
                    # The mock never answers, so the build stays parked.
                    await release.wait()
                    first_send_returned = True
                    return {}
                return {"snapshot_id": "snap2", "count": 3}
            return {}

        controller.app = MagicMock()
        controller.app.send_request = _send_request

        result = machine.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
        assert machine.state is OverlayState.POST_CLICK_SETTLING
        settle_batch = asyncio.ensure_future(
            controller._dispatch_overlay_effects(result.effects, trace_id="tr")
        )
        # Past the 30 ms settling deadline; the TIMEOUT closes the state.
        await asyncio.sleep(0.10)
        assert machine.state is OverlayState.CLOSED

        await controller.handle_overlay_command("show", "tr-show")
        # Long enough for a freed walk to start, far short of the 5 s send
        # deadline the stalled settle send would otherwise have to reach.
        await asyncio.sleep(0.20)
        walks_started = len(walks)
        stale_send_answered = first_send_returned

        release.set()
        await settle_batch
        await _settle(controller)
        return walks_started, stale_send_answered

    walks_started, stale_send_answered = asyncio.run(_run())

    assert not stale_send_answered, (
        "the stalled settle send must still be unanswered when the test "
        "samples; otherwise the second walk could have started simply "
        "because the first send finished"
    )
    assert walks_started == 2, (
        "a timed-out post_click_settling must wake its parked settle build so "
        "the next 'show numbers' can ISSUE its own start_overlay_walk "
        f"request; Logic had issued {walks_started} start_overlay_walk "
        "send(s), so the fresh request was still queued behind the dead "
        "settle request inside Logic"
    )


def test_show_numbers_during_settling_also_wakes_the_parked_settle_build():
    """'show numbers' in the gap must not queue behind the parked build.

    wh-overlay-slow-uia-stale-badges.2.2.4 (codex round 5), the command half.
    POST_CLICK_SETTLING's SHOW_NUMBERS cell unpins and walks fresh, so the
    settle build parked on its ``app.send_request`` await is superseded the
    moment the user speaks. Nothing woke it: ``handle_overlay_command`` called
    ``_abort_inflight_overlay_build`` only on its HIDE_NUMBERS arm. The fresh
    walk then waited out ``response_timeout_ms`` behind a dead request.

    The settling deadline is left at its 8000 ms default here on purpose, so
    the only thing that can leave the state is the spoken command.

    What this proves, and where the proof stops
    (wh-overlay-slow-uia-stale-badges.2.2.5, codex round 6). ``send_request``
    is a mock here, so this is a LOGIC-side proof: it shows Logic issues the
    second ``start_overlay_walk`` request instead of parking behind the dead
    one. It does not exercise ``app.py`` delivery or the Input process's single
    synchronous command loop. Once a frame reaches shared memory nothing
    withdraws it -- ``app.py`` checks ``_delivery_abandoned`` before the write
    and states outright that it cannot change after -- so an Input process
    already inside a hung UIA read stays there. That containment is
    wh-overlay-slow-uia-stale-badges.11 ("Contain the single command loop in
    the Input process: bounded queue, stale-command expiry, cancellation"), an
    open sibling bead, and it is deliberately not this branch's work.
    """

    machine = ClickOverlayStateMachine(settle_after_click=True)
    _drive_to_painted(machine, "snap")

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller.click_config.response_timeout_ms = 5000
        controller.click_config.screen_read_timeout_ms = 5000

        walks: list[object] = []
        release = asyncio.Event()
        first_send_returned = False

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            nonlocal first_send_returned
            if action == "start_overlay_walk":
                walks.append(params)
                if len(walks) == 1:
                    await release.wait()
                    first_send_returned = True
                    return {}
                return {"snapshot_id": "snap2", "count": 3}
            return {}

        controller.app = MagicMock()
        controller.app.send_request = _send_request

        result = machine.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
        assert machine.state is OverlayState.POST_CLICK_SETTLING
        settle_batch = asyncio.ensure_future(
            controller._dispatch_overlay_effects(result.effects, trace_id="tr")
        )
        # Long enough for the settle build to be parked on its send await.
        await asyncio.sleep(0.05)
        assert machine.state is OverlayState.POST_CLICK_SETTLING

        await controller.handle_overlay_command("show", "tr-show")
        await asyncio.sleep(0.20)
        walks_started = len(walks)
        stale_send_answered = first_send_returned

        release.set()
        await settle_batch
        await _settle(controller)
        return walks_started, stale_send_answered

    walks_started, stale_send_answered = asyncio.run(_run())

    assert not stale_send_answered, (
        "the stalled settle send must still be unanswered when the test "
        "samples; otherwise the fresh walk could have started simply because "
        "the first send finished"
    )
    assert walks_started == 2, (
        "'show numbers' during post_click_settling must wake the parked "
        "settle build so Logic can ISSUE its own start_overlay_walk "
        "request; "
        f"Logic had issued {walks_started} start_overlay_walk send(s), so the "
        "fresh request was still queued behind the dead settle request inside "
        "Logic"
    )


def test_the_build_abort_refuses_to_wake_a_build_the_machine_still_wants():
    """The abort is pair-aware: a live build must never be woken.

    wh-overlay-slow-uia-stale-badges.2.2.4. ``_abort_inflight_overlay_build``
    is now called after every transition out of POST_CLICK_SETTLING, not only
    after a hide, so it must decide for itself whether the parked send is
    still consumable. Waking a live build would abandon a real read and paint
    nothing. The parked pair is what makes that decision possible.
    """

    machine = ClickOverlayStateMachine(settle_after_click=True)
    _drive_to_painted(machine, "snap")
    controller = _controller(machine=machine)

    live_pair = (machine.overlay_session_id, machine.paint_generation)
    abort = asyncio.Event()
    controller._overlay_build_abort = abort
    controller._overlay_build_abort_pair = live_pair

    controller._abort_inflight_overlay_build()
    assert not abort.is_set(), (
        "the machine is still in a live state at the parked pair, so this "
        "build's answer is still consumable and must not be abandoned"
    )

    # The same machine, one generation on: the parked read is now stale.
    controller._overlay_build_abort_pair = (live_pair[0], live_pair[1] - 1)
    controller._abort_inflight_overlay_build()
    assert abort.is_set(), (
        "a parked build at a superseded generation must be woken, or it "
        "holds the effect lock for the rest of response_timeout_ms"
    )


# ----------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.24: the four remaining supersede paths
# ----------------------------------------------------------------------
def _wire_one_stalled_walk(controller):
    """Give the controller an app whose FIRST overlay walk never answers.

    The first ``start_overlay_walk`` blocks on the returned event, so the
    build that issued it stays parked on its Input round trip holding
    ``_overlay_effect_lock`` -- the exact position a superseded build is in
    when the machine leaves it behind. Every later walk answers at once with
    a well-formed response echoing the pair it was asked for, so the freed
    build completes the way a real one would; every other action answers with
    an empty dict.

    Returns ``(walks, release, answered)``: ``walks`` collects the params of
    each walk request Logic actually issued, ``release`` frees the stalled
    first send, and ``answered[0]`` turns True only if that first send ever
    returned normally.
    """

    walks: list[dict] = []
    release = asyncio.Event()
    answered = [False]

    async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
        if action == "start_overlay_walk":
            asked = dict(params or {})
            walks.append(asked)
            if len(walks) == 1:
                await release.wait()
                answered[0] = True
                return {}
            return _walk_response(
                "snap-fresh",
                asked.get("overlay_session_id"),
                asked.get("paint_generation"),
                1, 2, 3,
            )
        return {}

    controller.app = MagicMock()
    controller.app.send_request = _send_request
    return walks, release, answered


def _controller_with_a_parked_walk(machine):
    """Build a controller wired for the four supersede tests below."""

    controller = _controller(machine=machine)
    controller.loop = asyncio.get_running_loop()
    controller.background_tasks = []
    # Far above every sleep in these tests, so a second walk that starts can
    # only have started because the parked one was woken.
    controller.click_config.response_timeout_ms = 5000
    controller.click_config.screen_read_timeout_ms = 5000
    return controller


async def _assert_the_supersede_freed_the_next_walk(
    controller, machine, first_effects, supersede, *, expected_first_state,
):
    """Park a walk build, run ``supersede``, and sample what Logic issued.

    ``first_effects`` is the batch that dispatches the build which will park.
    ``supersede`` is an async callable that delivers the event superseding it.
    Returns ``(walks_started, stale_send_answered)`` sampled after the
    supersede, with the stalled send released and every background task
    drained before the return.
    """

    walks, release, answered = _wire_one_stalled_walk(controller)
    assert machine.state is expected_first_state
    first_batch = asyncio.ensure_future(
        controller._dispatch_overlay_effects(first_effects, trace_id="tr-1")
    )
    # Long enough for the first build to reach its send await and park.
    await asyncio.sleep(0.05)
    assert len(walks) == 1, (
        "test setup: the first build must be parked on its walk send before "
        f"the supersede; Logic had issued {len(walks)} walk send(s)"
    )

    await supersede()
    # Long enough for a freed walk to start, far short of the 5 s deadline
    # the stalled send would otherwise have to reach.
    await asyncio.sleep(0.20)
    walks_started, stale_send_answered = len(walks), answered[0]

    release.set()
    await first_batch
    await _settle(controller)
    return walks_started, stale_send_answered


def test_show_numbers_during_walk_in_flight_wakes_the_parked_walk_build():
    """'show numbers' spoken during a walk must not queue behind the old walk.

    wh-overlay-slow-uia-stale-badges.24, path 1 of 4: WALK_IN_FLIGHT
    SHOW_NUMBERS through ``_restart_walk``. The cell bumps the generation and
    dispatches a fresh build, which makes the parked one stale, but the
    ``_abort_inflight_overlay_build`` call in ``handle_overlay_command`` ran
    only when the machine had just left POST_CLICK_SETTLING. So the fresh walk
    queued behind the dead one on ``_overlay_effect_lock`` and did not start
    until ``response_timeout_ms`` expired -- a key ``ui/click_config.py``
    validates with a minimum of 100 and no ceiling.

    Same Logic-side proof, and the same limit on it, as the settle tests
    above: ``send_request`` is a mock, so this shows Logic ISSUES the second
    request instead of parking behind the dead one. Input's single synchronous
    command loop is wh-overlay-slow-uia-stale-badges.11 and is not this
    branch's work.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        controller = _controller_with_a_parked_walk(machine)
        first = machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))

        async def _supersede():
            await controller.handle_overlay_command("show", "tr-show")

        return await _assert_the_supersede_freed_the_next_walk(
            controller, machine, first.effects, _supersede,
            expected_first_state=OverlayState.WALK_IN_FLIGHT,
        )

    walks_started, stale_send_answered = asyncio.run(_run())

    assert not stale_send_answered, (
        "the stalled first walk must still be unanswered when the test "
        "samples; otherwise the second walk could have started simply "
        "because the first send finished"
    )
    assert walks_started == 2, (
        "a re-said 'show numbers' during walk_in_flight must wake the walk "
        "build it superseded so Logic can ISSUE its own start_overlay_walk; "
        f"Logic had issued {walks_started} walk send(s), so the fresh request "
        "was still queued behind the dead one inside Logic"
    )


def test_focus_change_during_walk_in_flight_wakes_the_parked_walk_build():
    """A focus change during a walk must not queue the restart behind it.

    wh-overlay-slow-uia-stale-badges.24, path 2 of 4: WALK_IN_FLIGHT
    FOCUS_CHANGE through ``_restart_walk``, on the ``_apply_overlay_event``
    side. The focus hook fires this whenever the user switches window while a
    walk is still running, which is the common case for a slow UIA provider.
    The cell supersedes the parked build; nothing woke it, because the
    ``_abort_inflight_overlay_build`` call on that path ran only for a
    transition out of POST_CLICK_SETTLING.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        controller = _controller_with_a_parked_walk(machine)
        first = machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))

        async def _supersede():
            controller._apply_overlay_event(
                OverlayEvent(OverlayEventKind.FOCUS_CHANGE),
                source="test-24-walk-focus",
            )

        return await _assert_the_supersede_freed_the_next_walk(
            controller, machine, first.effects, _supersede,
            expected_first_state=OverlayState.WALK_IN_FLIGHT,
        )

    walks_started, stale_send_answered = asyncio.run(_run())

    assert not stale_send_answered, (
        "the stalled first walk must still be unanswered when the test "
        "samples; otherwise the restart could have started simply because "
        "the first send finished"
    )
    assert walks_started == 2, (
        "a focus change during walk_in_flight must wake the walk build it "
        "superseded so the restart can ISSUE its own start_overlay_walk; "
        f"Logic had issued {walks_started} walk send(s), so the restart was "
        "still queued behind the dead one inside Logic"
    )


def test_show_numbers_during_refresh_in_flight_wakes_the_parked_build():
    """'show numbers' during a refresh must not queue behind the old refresh.

    wh-overlay-slow-uia-stale-badges.24, path 3 of 4: REFRESH_IN_FLIGHT
    SHOW_NUMBERS through ``_refresh_supersede``. The user is looking at badges
    while a refresh walk runs, says 'show numbers' again, and the cell bumps
    the generation and dispatches a fresh build. The refresh build parked on
    its Input round trip is now stale and nothing woke it.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        controller = _controller_with_a_parked_walk(machine)
        _drive_to_painted(machine, "snap-24c")
        first = machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))

        async def _supersede():
            await controller.handle_overlay_command("show", "tr-show")

        return await _assert_the_supersede_freed_the_next_walk(
            controller, machine, first.effects, _supersede,
            expected_first_state=OverlayState.REFRESH_IN_FLIGHT,
        )

    walks_started, stale_send_answered = asyncio.run(_run())

    assert not stale_send_answered, (
        "the stalled refresh walk must still be unanswered when the test "
        "samples; otherwise the second walk could have started simply "
        "because the first send finished"
    )
    assert walks_started == 2, (
        "a re-said 'show numbers' during refresh_in_flight must wake the "
        "refresh build it superseded so Logic can ISSUE its own "
        f"start_overlay_walk; Logic had issued {walks_started} walk send(s), "
        "so the fresh request was still queued behind the dead one inside "
        "Logic"
    )


def test_focus_change_during_refresh_in_flight_wakes_the_parked_build():
    """A second focus change must not queue its refresh behind the first.

    wh-overlay-slow-uia-stale-badges.24, path 4 of 4: REFRESH_IN_FLIGHT
    FOCUS_CHANGE through ``_refresh_supersede``, on the
    ``_apply_overlay_event`` side. A dialog opening over a painted overlay
    fires one focus change, and the dialog taking focus fires another, so two
    arrive in a row routinely. The second supersedes the first refresh build
    while it is still parked on its Input round trip.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        controller = _controller_with_a_parked_walk(machine)
        _drive_to_painted(machine, "snap-24d")
        first = machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))

        async def _supersede():
            controller._apply_overlay_event(
                OverlayEvent(OverlayEventKind.FOCUS_CHANGE),
                source="test-24-refresh-focus",
            )

        return await _assert_the_supersede_freed_the_next_walk(
            controller, machine, first.effects, _supersede,
            expected_first_state=OverlayState.REFRESH_IN_FLIGHT,
        )

    walks_started, stale_send_answered = asyncio.run(_run())

    assert not stale_send_answered, (
        "the stalled refresh walk must still be unanswered when the test "
        "samples; otherwise the second refresh could have started simply "
        "because the first send finished"
    )
    assert walks_started == 2, (
        "a second focus change during refresh_in_flight must wake the "
        "refresh build it superseded so the new refresh can ISSUE its own "
        f"start_overlay_walk; Logic had issued {walks_started} walk send(s), "
        "so the second refresh was still queued behind the dead one inside "
        "Logic"
    )


def test_the_settling_deadline_alone_wakes_the_parked_settle_build():
    """The settling TIMEOUT must free the effect lock with no command after it.

    wh-overlay-slow-uia-stale-badges.24, and the reason this test exists at
    all. ``test_a_timed_out_settle_does_not_park_the_next_show_numbers`` above
    proves the same wake end to end, but it says 'show numbers' after the
    deadline -- and .24 made the ``handle_overlay_command`` wake
    unconditional, so that command now wakes the parked build by itself. The
    older test therefore stayed green when the ``_apply_overlay_event`` wake
    was removed: the fixture had become covered twice, and the mutation gate
    reported it as a survivor.

    This test removes the second cover. Nothing is spoken after the deadline,
    so the settling TIMEOUT arriving through ``_apply_overlay_event`` is the
    only thing that can wake the settle build parked on its Input round trip.
    The observation is the batch itself: it holds ``_overlay_effect_lock``
    until the parked send ends, so a batch that has FINISHED while its send is
    still unanswered is the wake, and a batch still running would be waiting
    out the 5 s ``screen_read_timeout_ms`` with the lock held.
    """

    async def _run():
        machine = ClickOverlayStateMachine(
            settle_after_click=True, settle_deadline_ms=30,
        )
        _drive_to_painted(machine, "snap-24e")
        controller = _controller_with_a_parked_walk(machine)
        walks, release, answered = _wire_one_stalled_walk(controller)

        result = machine.apply(OverlayEvent(OverlayEventKind.CLICK_COMPLETE))
        assert machine.state is OverlayState.POST_CLICK_SETTLING
        settle_batch = asyncio.ensure_future(
            controller._dispatch_overlay_effects(result.effects, trace_id="tr")
        )
        # Long enough for the settle build to park on its send await, and
        # short of the 30 ms settling deadline.
        await asyncio.sleep(0.01)
        assert len(walks) == 1, (
            "test setup: the settle build must be parked on its walk send "
            f"before the deadline; Logic had issued {len(walks)} walk send(s)"
        )

        # Past the settling deadline, and far short of the 5 s the stalled
        # send would otherwise hold the lock for. Nothing is spoken here.
        await asyncio.sleep(0.30)
        state_now = machine.state
        batch_finished = settle_batch.done()
        stale_send_answered = answered[0]

        release.set()
        await settle_batch
        await _settle(controller)
        return state_now, batch_finished, stale_send_answered

    state_now, batch_finished, stale_send_answered = asyncio.run(_run())

    assert state_now is OverlayState.CLOSED, (
        "test setup: settle_deadline_ms must have ended post_click_settling "
        f"on its own; the machine was still {state_now.value}"
    )
    assert not stale_send_answered, (
        "the stalled settle send must still be unanswered when the test "
        "samples; otherwise the batch could have finished simply because the "
        "send finished"
    )
    assert batch_finished, (
        "the settling deadline must wake the settle build it left behind, so "
        "the batch releases _overlay_effect_lock at once; the batch was still "
        "running, which means it was holding the lock for the rest of "
        "screen_read_timeout_ms with nothing the machine could use"
    )


# ---------------------------------------------------------------------------
# wh-overlay-timer-cancel-race: a timeout callback already ready in the loop
# must not act once a same-pair transition has committed.
#
# Four transitions keep the (overlay_session_id, paint_generation) pair the
# timer was armed for, so the machine's generation gate cannot reject the
# TIMEOUT. Each of them emits CANCEL_TIMER, but that effect is dispatched as a
# task and cannot run until a later loop turn -- and a callback already in the
# ready queue runs in this one. Measured on the machine before these tests were
# written: family 1 CLOSES a successful build; families 2, 3 and 4 reach the
# invalid cell and enter ERROR.
# ---------------------------------------------------------------------------


class _TimerArmCapture:
    """A ``controller.loop`` stand-in that CAPTURES timer arms, scheduling none.

    Every attribute but ``call_later`` forwards to the real running loop. A
    ``call_later`` records ``(delay, callback, args)`` and hands back a Mock
    handle, so the TEST decides the instant a callback runs. That instant is
    the whole subject here: production's callback is already in the loop's
    ready queue when the transition commits, and asyncio runs a ready handle in
    that same batch, while the CANCEL_TIMER that would have stopped it waits
    for a turn it does not get until later.

    Nothing reaches the real loop, so a captured callback cannot fire late and
    cannot hang a test.
    """

    def __init__(self, loop) -> None:
        self._loop = loop
        self.arms: list[tuple[float, Any, tuple]] = []

    def __getattr__(self, name):
        return getattr(self._loop, name)

    def call_later(self, delay, callback, *args):
        self.arms.append((delay, callback, args))
        return MagicMock()

    def fire(self, index: int = -1) -> None:
        """Run one captured callback, as the ready queue would have."""

        _delay, callback, args = self.arms[index]
        callback(*args)


def _arm_timer_effect(result):
    """Return the single ARM_TIMER effect in a machine result."""

    arms = [e for e in result.effects if e.kind is EffectKind.ARM_TIMER]
    assert len(arms) == 1, f"expected exactly one ARM_TIMER, got {len(arms)}"
    return arms[0]


def _race_controller(machine, loop):
    """A controller whose timer arms are captured and whose effects never run.

    ``_perform_overlay_effects`` is a Mock, and that is what holds the test
    inside the race window: the CANCEL_TIMER the transition emits is never
    dispatched, which is the production position at the moment the ready
    callback runs.
    """

    controller = _controller(machine=machine)
    controller.loop = _TimerArmCapture(loop)
    controller.background_tasks = []
    controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
    return controller


def _assert_timer_bookkeeping_cleared(controller) -> None:
    """The arm really did expire, so its own bookkeeping is spent."""

    assert controller._overlay_timer is None
    assert controller._overlay_timer_pair is None
    assert controller._overlay_armed_timer_state is None


def test_a_ready_walk_timeout_does_not_close_a_successful_build():
    """Family 1: WALK_IN_FLIGHT -> PAINT_IN_FLIGHT keeps the pair.

    The walk deadline is armed for WALK_IN_FLIGHT at (sid, gen). The build
    answers and BUILD_RESPONSE commits PAINT_IN_FLIGHT at that SAME pair. A
    walk callback already ready in the loop is then read as a PAINT timeout,
    and it closes the overlay the build had just succeeded in producing.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        controller = _race_controller(machine, asyncio.get_running_loop())
        show = machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        sid, gen = machine.overlay_session_id, machine.paint_generation
        arm = _arm_timer_effect(show)
        assert arm.timer_state is OverlayState.WALK_IN_FLIGHT
        controller._overlay_arm_timer(arm, "tr-race-walk-paint")

        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-race-1",
            ),
            source="test-build-response",
        )
        assert machine.state is OverlayState.PAINT_IN_FLIGHT
        assert (
            machine.overlay_session_id, machine.paint_generation,
        ) == (sid, gen), "test setup: the transition must keep the pair"

        controller.loop.fire()
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.PAINT_IN_FLIGHT, (
        "the walk deadline acted after the machine had left WALK_IN_FLIGHT; "
        "a successful build was closed as though the paint had timed out"
    )
    _assert_timer_bookkeeping_cleared(controller)


def test_a_ready_walk_timeout_does_not_error_a_pause_during_the_walk():
    """Family 2: WALK_IN_FLIGHT -> PAUSED keeps the pair.

    MIC_PAUSE during a walk changes nothing on its own; the build result is
    what commits PAUSED, at the same pair. PAUSED x TIMEOUT is an invalid cell,
    so a walk callback already ready in the loop drives the machine to ERROR.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        controller = _race_controller(machine, asyncio.get_running_loop())
        show = machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        sid, gen = machine.overlay_session_id, machine.paint_generation
        arm = _arm_timer_effect(show)
        assert arm.timer_state is OverlayState.WALK_IN_FLIGHT
        controller._overlay_arm_timer(arm, "tr-race-walk-paused")

        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.MIC_PAUSE), source="test-mic-pause",
        )
        assert machine.state is OverlayState.WALK_IN_FLIGHT
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-race-2",
            ),
            source="test-build-response",
        )
        assert machine.state is OverlayState.PAUSED
        assert (
            machine.overlay_session_id, machine.paint_generation,
        ) == (sid, gen), "test setup: the transition must keep the pair"

        controller.loop.fire()
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.PAUSED, (
        "the walk deadline acted after the machine had paused; PAUSED x "
        "TIMEOUT is an invalid cell, so the overlay entered ERROR"
    )
    _assert_timer_bookkeeping_cleared(controller)


def test_a_ready_paint_timeout_does_not_error_a_completed_paint():
    """Family 3: PAINT_IN_FLIGHT -> PAINTED keeps the pair.

    The paint deadline is armed for PAINT_IN_FLIGHT. The display acks, and
    PAINT_ACK(PAINTED) commits PAINTED at the same pair. PAINTED x TIMEOUT is
    an invalid cell, so a paint callback already ready in the loop turns a
    finished paint into ERROR.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        controller = _race_controller(machine, asyncio.get_running_loop())
        machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        sid, gen = machine.overlay_session_id, machine.paint_generation
        built = machine.apply(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-race-3",
            )
        )
        arm = _arm_timer_effect(built)
        assert arm.timer_state is OverlayState.PAINT_IN_FLIGHT
        controller._overlay_arm_timer(arm, "tr-race-paint-painted")

        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            ),
            source="test-paint-ack",
        )
        assert machine.state is OverlayState.PAINTED
        assert (
            machine.overlay_session_id, machine.paint_generation,
        ) == (sid, gen), "test setup: the transition must keep the pair"

        controller.loop.fire()
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.PAINTED, (
        "the paint deadline acted after the paint had been acknowledged; "
        "PAINTED x TIMEOUT is an invalid cell, so the overlay entered ERROR"
    )
    _assert_timer_bookkeeping_cleared(controller)


def test_a_ready_refresh_timeout_does_not_error_a_resolved_refresh():
    """Family 4: REFRESH_IN_FLIGHT -> PAINTED keeps the pair.

    The refresh deadline guards REFRESH_IN_FLIGHT, whose build and paint both
    answer at the bumped pair. The paint ack commits PAINTED there, so a
    refresh callback already ready in the loop turns a recovered overlay into
    ERROR. This is the late half of the refresh window: the sibling tests above
    fire the refresh deadline while the refresh is still in flight, which is
    the legitimate fall-back and must keep working.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        controller = _race_controller(machine, asyncio.get_running_loop())
        _drive_to_painted(machine, "snap-race-4a")
        refreshed = machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
        sid, gen = machine.overlay_session_id, machine.paint_generation
        arm = _arm_timer_effect(refreshed)
        assert arm.timer_state is OverlayState.REFRESH_IN_FLIGHT
        controller._overlay_arm_timer(arm, "tr-race-refresh-painted")

        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
                paint_generation=gen, snapshot_id="snap-race-4b",
            ),
            source="test-build-response",
        )
        assert machine.state is OverlayState.REFRESH_IN_FLIGHT
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
                paint_generation=gen, paint_state=PaintAckState.PAINTED,
            ),
            source="test-paint-ack",
        )
        assert machine.state is OverlayState.PAINTED
        assert (
            machine.overlay_session_id, machine.paint_generation,
        ) == (sid, gen), "test setup: the transition must keep the pair"

        controller.loop.fire()
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.PAINTED, (
        "the refresh deadline acted after the refresh had resolved; a "
        "recovered overlay was driven into ERROR"
    )
    _assert_timer_bookkeeping_cleared(controller)


def test_a_stale_callback_does_not_clear_a_later_timers_bookkeeping():
    """The second requirement: an obsolete callback must touch nothing.

    A new arm can carry the SAME pair and the SAME state as the arm it
    replaces, so neither the pair nor the armed state can tell them apart. The
    per-arm id can. Here the walk deadline is armed twice at the same pair and
    state; the first callback then runs. It must leave the second arm's
    bookkeeping intact, and it must not feed a TIMEOUT.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        controller = _race_controller(machine, asyncio.get_running_loop())
        show = machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        arm = _arm_timer_effect(show)
        controller._overlay_arm_timer(arm, "tr-race-first-arm")
        first_index = len(controller.loop.arms) - 1
        controller._overlay_arm_timer(arm, "tr-race-second-arm")
        live_handle = controller._overlay_timer

        controller.loop.fire(first_index)
        return controller, machine, live_handle

    controller, machine, live_handle = asyncio.run(_run())
    assert machine.state is OverlayState.WALK_IN_FLIGHT, (
        "the replaced arm's callback fed a TIMEOUT for a timer that had "
        "already been superseded"
    )
    assert controller._overlay_timer is live_handle, (
        "the replaced arm's callback cleared the LIVE timer's handle; the "
        "second arm can no longer be cancelled"
    )
    assert controller._overlay_armed_timer_state is OverlayState.WALK_IN_FLIGHT


def test_a_timer_armed_with_no_state_does_not_fire():
    """A timer armed with no state cannot prove where the machine was.

    ``Effect.timer_state`` is Optional and every ARM_TIMER the machine builds
    today names a state, so this is the type's open case rather than a path
    production reaches. A timer armed with None carries no evidence that the
    machine is still in the state the arm was made for, so it feeds nothing.
    Written after the fix; its red proof is the ``none-armed-state-fires``
    mutation in tests/mutation_gate_overlay_timer_state_check.py.
    """

    async def _run():
        machine = ClickOverlayStateMachine()
        controller = _race_controller(machine, asyncio.get_running_loop())
        show = machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        sid, gen = machine.overlay_session_id, machine.paint_generation
        armed = _arm_timer_effect(show)
        stateless = Effect(
            kind=EffectKind.ARM_TIMER,
            overlay_session_id=sid,
            paint_generation=gen,
            timer_state=None,
            duration_ms=armed.duration_ms,
        )
        controller._overlay_arm_timer(stateless, "tr-race-no-state")
        assert controller._overlay_armed_timer_state is None

        controller.loop.fire()
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.WALK_IN_FLIGHT, (
        "a timer armed with no state fed a TIMEOUT; nothing about that arm "
        "shows the machine is still where it was when the timer started"
    )
    _assert_timer_bookkeeping_cleared(controller)


# ---------------------------------------------------------------------------
# wh-overlay-rewalk-after-filter.1.3: the pending build target.
#
# ``_overlay_tracked_identity`` names the LATEST PIN, and the pin happens only
# after a build response comes back. A Pattern Manager tree change that
# arrives WHILE the build is in flight therefore had no truthful window to be
# judged against: on the first session of a run the field is still None, and
# on a cross-window refresh it still names the window that lost the focus.
# ``_overlay_dispatch_build`` now samples the foreground once, at the single
# build funnel, into ``_overlay_pending_build_identity``. Three paths end it:
# the pin consumes it, entry to closed clears it, and the next dispatch
# replaces it.
# ---------------------------------------------------------------------------


def test_build_dispatch_records_the_window_the_build_is_for():
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation
    seen: dict[str, Any] = {}

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
            return_value="IDENTITY"
        )

        def _respond(action, params):
            if action == "start_overlay_walk":
                # Read at the moment of the send: the walk is in flight, and
                # this is exactly when a filter keystroke can arrive.
                seen["pending"] = getattr(
                    controller, "_overlay_pending_build_identity",
                    "MISSING",
                )
                return _walk_response("snap-w", sid, gen, 1)
            return _pin_response(action, params)

        _wire_app(controller, _respond)
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.SHOW_NUMBERS,
        )
        await controller._dispatch_overlay_effects((effect,), trace_id="tr")
        await _settle(controller)

    asyncio.run(_run())
    assert seen["pending"] == "IDENTITY"


def test_auto_open_build_dispatch_records_the_window_too():
    # AUTO_OPEN repaints a snapshot that was walked earlier, but it is still a
    # build the machine can supersede, so it owes the same target.
    machine = ClickOverlayStateMachine()
    machine.apply(
        OverlayEvent(OverlayEventKind.AUTO_OPEN, snapshot_id="snap-a")
    )
    sid, gen = machine.overlay_session_id, machine.paint_generation
    seen: dict[str, Any] = {}

    async def _run():
        controller = _controller(machine=machine)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
            return_value="IDENTITY"
        )

        def _respond(action, params):
            if action == "show_numbered_overlay":
                seen["pending"] = getattr(
                    controller, "_overlay_pending_build_identity",
                    "MISSING",
                )
                return _show_response("snap-a", sid, gen, 1)
            return _pin_response(action, params)

        _wire_app(controller, _respond)
        effect = Effect(
            kind=EffectKind.DISPATCH_BUILD,
            overlay_session_id=sid,
            paint_generation=gen,
            build_reason=BuildReason.AUTO_OPEN,
            snapshot_id="snap-a",
        )
        await controller._dispatch_overlay_effects((effect,), trace_id="tr")
        await asyncio.sleep(0)
        await asyncio.gather(*controller.background_tasks)

    asyncio.run(_run())
    assert seen["pending"] == "IDENTITY"


def test_pin_consumes_the_pending_build_target():
    controller = _controller()
    _wire_app(controller, lambda a, p: _pin_response(a, p))
    controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
        return_value="IDENTITY"
    )
    controller._overlay_pending_build_identity = "PENDING"
    effect = Effect(
        kind=EffectKind.PIN_SNAPSHOT,
        overlay_session_id=7,
        paint_generation=0,
        snapshot_id="snap-x",
    )
    asyncio.run(
        controller._dispatch_overlay_effects((effect,), trace_id="tr")
    )
    # The pin is the moment the tracked identity becomes truthful, so the
    # pending target has no further use.
    assert controller._overlay_tracked_identity == "IDENTITY"
    assert controller._overlay_pending_build_identity is None


def test_entry_to_closed_clears_the_pending_build_target():
    controller = _controller()
    controller.click_overlay_state.state = OverlayState.CLOSED
    controller._overlay_tracked_identity = "IDENTITY"
    controller._overlay_pending_build_identity = "PENDING"

    controller._reconcile_overlay_tracked_identity()

    assert controller._overlay_tracked_identity is None
    assert controller._overlay_pending_build_identity is None


# ---------------------------------------------------------------------------
# wh-overlay-rewalk-after-filter.1.4: a tree change queued ahead of the
# AUTO_OPEN effect task.
#
# ``_perform_auto_open_ambiguous`` commits closed -> walk_in_flight
# synchronously, but ``_perform_overlay_effects`` only SCHEDULES the effect
# batch, and the pending build target is sampled inside that batch. A
# Pattern Manager tree change already waiting on the loop runs in between.
# AUTO_OPEN repaints the snapshot the ambiguous click walked BEFORE the
# change, so a dropped event paints the pre-filter badges. The pair of tests
# below differs only in when the event is delivered.
# ---------------------------------------------------------------------------


def _replay_auto_open_with_a_tree_change(*, event_ahead_of_effects: bool):
    """Drive a first-session AUTO_OPEN with one Pattern Manager tree change.

    ``event_ahead_of_effects`` True queues the event with ``loop.call_soon``
    BEFORE the ambiguous-click reply is handled, so it runs after AUTO_OPEN
    commits walk_in_flight but before the scheduled effect task starts --
    the position of a GUI listener callback that is already ready on the
    Logic loop. False delivers the same event from inside the
    show_numbered_overlay send, after the effect task has sampled the
    foreground window.

    Returns the ordered actions sent to Input, the ``snapshot_id`` of every
    ``paint_overlay`` put on the GUI queue, and the machine's final state.
    """
    from services.wheelhouse.overlay_focus_hooks import (
        FocusChangeDebouncer,
        ForegroundIdentity,
    )

    result: dict[str, Any] = {}

    async def _run():
        controller = _controller()
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        # A first session: nothing pinned, no build target recorded yet.
        assert controller._overlay_tracked_identity is None
        assert controller._overlay_pending_build_identity is None
        controller._capture_overlay_foreground_identity = MagicMock(  # type: ignore[method-assign]
            return_value=ForegroundIdentity(
                hwnd=4242, pid=4321, process_name="python.exe",
                window_creation_time=7,
            )
        )
        controller._pm_tree_change_last_hwnd = 0
        controller._pm_tree_change_last_sequence = 0
        controller._overlay_focus_debouncer = FocusChangeDebouncer(
            debounce_ms=0
        )
        tree_event = {
            "action": "pattern_manager_tree_changed",
            "hwnd": 4242,
            "sequence": 1,
        }

        def _deliver_tree_change():
            controller._handle_pattern_manager_tree_changed(tree_event)

        def _respond(action, params):
            sid = params.get("overlay_session_id", 0)
            gen = params.get("paint_generation", 0)
            if action == "show_numbered_overlay":
                if not event_ahead_of_effects:
                    _deliver_tree_change()
                # AUTO_OPEN re-serves the snapshot the ambiguous click
                # walked, which predates the tree change.
                return _show_response("pre-filter", sid, gen, 1, 2)
            if action == "start_overlay_walk":
                return _walk_response("post-filter", sid, gen, 1)
            return _pin_response(action, params)

        sent = _wire_app(controller, _respond)
        if event_ahead_of_effects:
            controller.loop.call_soon(_deliver_tree_change)
        response = ClickElementResponse.from_dict(
            _ambiguous_response(
                snapshot_id="pre-filter",
                item_ids=("pre-filter-item-1", "pre-filter-item-2"),
            )
        )
        assert controller._perform_auto_open_ambiguous(
            response, "Cancel", "tr-1-4",
        )
        await _settle(controller)
        machine = controller.click_overlay_state
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK,
                overlay_session_id=machine.overlay_session_id,
                paint_generation=machine.paint_generation,
                paint_state=PaintAckState.PAINTED,
            ),
            source="test GUI ack",
        )
        await _settle(controller)
        result["sent"] = [action for action, _params in sent]
        result["paints"] = [
            item.get("snapshot_id")
            for item in _gui_items(controller)
            if item.get("action") == "paint_overlay"
        ]
        result["state"] = machine.state
        for name in ("_overlay_timer", "_overlay_keepalive_timer"):
            timer = getattr(controller, name, None)
            if timer is not None:
                timer.cancel()

    asyncio.run(_run())
    return result


def test_tree_change_queued_ahead_of_the_auto_open_build_is_not_dropped():
    """wh-overlay-rewalk-after-filter.1.4: an event queued before the build.

    Ordering pinned: the ambiguous click commits AUTO_OPEN (closed ->
    walk_in_flight) synchronously; a Pattern Manager tree change that was
    already ready on the Logic loop runs NEXT; only then does the scheduled
    effect task start and dispatch show_numbered_overlay. The event is for
    the foreground window, so it must be accepted: the build restarts as a
    fresh walk, and the badges that reach the screen come from the
    post-change tree, not from the snapshot the click walked before it.
    """
    result = _replay_auto_open_with_a_tree_change(event_ahead_of_effects=True)

    assert "start_overlay_walk" in result["sent"], (
        "tree change queued ahead of the AUTO_OPEN effect task was dropped: "
        f"no superseding walk was sent (sent={result['sent']})"
    )
    assert result["state"] is OverlayState.PAINTED
    assert result["paints"] and result["paints"][-1] == "post-filter", (
        "AUTO_OPEN painted the pre-filter snapshot after the tree change "
        f"(paints={result['paints']})"
    )


def test_tree_change_after_the_auto_open_build_started_is_accepted():
    """wh-overlay-rewalk-after-filter.1.4 control: the same event, delivered late.

    Ordering pinned: the effect task has already started and sampled the
    foreground window when the tree change arrives (inside the
    show_numbered_overlay send). Everything else matches the test above, so
    this passing while that one fails shows the only difference is the
    delivery order.
    """
    result = _replay_auto_open_with_a_tree_change(
        event_ahead_of_effects=False
    )

    assert "start_overlay_walk" in result["sent"], (
        f"no superseding walk was sent (sent={result['sent']})"
    )
    assert result["sent"].index("start_overlay_walk") > result["sent"].index(
        "show_numbered_overlay"
    )
    assert result["state"] is OverlayState.PAINTED
    assert result["paints"] and result["paints"][-1] == "post-filter", (
        f"paints={result['paints']}"
    )
