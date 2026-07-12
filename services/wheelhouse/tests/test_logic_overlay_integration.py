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
    controller._overlay_hold_timer = None
    controller._overlay_keepalive_timer = None
    controller._overlay_focus_debouncer = MagicMock()
    controller._overlay_settle_handle = None
    controller._overlay_focus_hooks = None
    controller._overlay_destroy_hook_active = False
    controller._overlay_tracked_identity = None
    controller._overlay_snapshot_window_identity = {}
    controller._overlay_pending_postclick_refresh = None
    controller._overlay_auto_open_filter = None
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

    async def _send_request(action, params=None, timeout_s=None):
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

    async def _raise(action, params=None, timeout_s=None):
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


def test_dispatch_snapshot_item_click_schedules_send():
    """The synchronous voice-path seam schedules the real async send."""

    controller = _controller()

    async def _run():
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        captured = {}

        async def _fake_send(*, snapshot_id, item_id, trace_id,
                             overlay_dispatch_pair=None):
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
    controller = _controller(cache=cache)
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

        def _dispatch(*, snapshot_id, item_id, trace_id):
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

        def _dispatch(*, snapshot_id, item_id, trace_id):
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


def test_ok_click_when_paused_is_noop_and_does_not_raise():
    """A CLICK_COMPLETE for a click dispatched at the current pair while the
    machine is now PAUSED must be a Logic-side no-op (not driving the machine's
    _invalid path to error). The state-required gate, not pair-match alone,
    keeps a same-generation paused overlay from being driven to error."""

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
                              overlay_dispatch_pair):
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
    assert machine.paint_generation == gen + 1
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
    assert machine.paint_generation == gen + 1
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
    # wh-n29v.118 / wh-n29v.120.1: the walk-start payload carries
    # 2 * response_timeout_ms so the GUI fallback outlasts the full success-path
    # latency (walk send_request + PIN_SNAPSHOT ack await, each bounded by
    # response_timeout_ms), not the walk alone. A fallback sized for the walk
    # alone would clear the cue during a slow/hung pin before the numbers paint.
    assert (
        cues[0]["walk_timeout_ms"]
        == controller.click_config.response_timeout_ms * 2
    )


def test_overlay_walk_cue_walk_timeout_covers_walk_plus_pin():
    """wh-n29v.120.1 regression: on the success path the cue is cleared by the
    GUI when paint_overlay arrives, and paint_overlay is enqueued only after the
    PIN_SNAPSHOT ack await (bounded by response_timeout_ms) that runs after the
    walk send_request (also bounded by response_timeout_ms). The walk-start cue
    must therefore carry a GUI fallback bound of 2 * response_timeout_ms so the
    fallback never fires before paint_overlay on a slow/hung pin.
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
    assert cues[0]["walk_timeout_ms"] == rt * 2
    # The carried bound must strictly exceed the single-walk bound that an
    # earlier slice used, which is the gap deepseek caught.
    assert cues[0]["walk_timeout_ms"] > rt


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

        async def _send_request(action, params=None, timeout_s=None):
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

        async def _send_request(action, params=None, timeout_s=None):
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
        swap = controller._overlay_proactive_swap
        assert swap is not None
        prior_id, swap_t = swap
        assert prior_id == "snap-old"
        assert swap_t == clock["t"]

    asyncio.run(_run())


def test_failed_proactive_refresh_records_no_swap_guard():
    async def _run():
        controller, machine, clock, sent, walk_config = (
            await _drive_proactive_cycle(refresh_ok=False)
        )
        assert controller._overlay_proactive_swap is None

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
        assert controller._overlay_proactive_swap is None

    asyncio.run(_run())
