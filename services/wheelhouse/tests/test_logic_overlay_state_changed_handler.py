"""Logic-side consumer tests for ``overlay_state_changed`` (wh-n29v.67).

The GUI emits ``overlay_state_changed { state, overlay_session_id,
paint_generation, monitor_ids, snapshot_id }`` on the
``commands_to_logic_queue`` after it applies / fails / clears a numbered-overlay
paint request. Before this slice, ``main.py``'s ``_build_gui_handler_map`` had no
route for the action, so ``_listen_for_gui_commands`` logged "Received unknown
action from GUI: overlay_state_changed" and dropped it -- the Logic-side paint-
acknowledgement state machine was never driven.

This slice adds:

  * ``LogicController._handle_overlay_state_changed(command)`` -- an async GUI
    handler that ``safe_parse``s the payload (wh-uf54: malformed payload logged
    and dropped, never raised into the listener), early-returns when the overlay
    is not active, maps the wire ``state`` string to the machine's
    ``PaintAckState``, builds a ``PAINT_ACK`` ``OverlayEvent`` carrying the
    wire's ``overlay_session_id`` / ``paint_generation`` / ``snapshot_id``, and
    applies it THROUGH ``_apply_overlay_event`` so the effect hand-off,
    debouncer reset, and destroy-hook reconciliation are reused.
  * a ``"overlay_state_changed"`` entry in ``_build_gui_handler_map`` that
    invokes the handler via ``create_task_with_error_handling``, matching the
    ``"snapshot_item_clicked"`` precedent's shape.

These tests build a ``LogicController`` via ``object.__new__`` to skip the heavy
``__init__`` and inject only the few attributes the handler touches
(``click_config``, ``click_overlay_state``, ``_perform_overlay_effects``). The
generation gate is the machine's OWN concern: the handler copies the wire pair
onto the ``OverlayEvent`` and the machine returns ``STALE_GENERATION`` with no
state change / no effects when it does not match. The handler does NOT
re-implement a generation check, so the stale case is asserted via the machine's
resulting state and the (non-)hand-off to ``_perform_overlay_effects``.
"""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import MagicMock

from services.wheelhouse.click_overlay_state import (
    ClickOverlayStateMachine,
    OverlayEvent,
    OverlayEventKind,
    OverlayState,
)
from services.wheelhouse.main import LogicController
from services.wheelhouse.shared.overlay_state_changed import (
    ACTION_NAME,
    OverlayStateChangedEvent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _controller(*, enabled: bool = True, overlay_effective: bool = True):
    """Build a bare LogicController with only the overlay-handler attributes.

    Skips the heavy ``__init__`` via ``object.__new__`` (the wh-n29v test
    precedent) and injects:

      * ``click_config`` -- a MagicMock with ``enabled`` /
        ``overlay_enabled_effective`` so the active-gate is exercised.
      * ``click_overlay_state`` -- a real ``ClickOverlayStateMachine`` so the
        PAINT_ACK transition and the generation gate run for real.
      * ``_perform_overlay_effects`` -- a MagicMock that captures the effects
        handed off by ``_apply_overlay_event``.
    """

    controller = object.__new__(LogicController)
    controller.click_config = MagicMock()
    controller.click_config.enabled = enabled
    controller.click_config.overlay_enabled_effective = overlay_effective
    controller.click_config.overlay_invalid_key = None
    controller.click_overlay_state = ClickOverlayStateMachine()
    controller._perform_overlay_effects = MagicMock()  # type: ignore[method-assign]
    # _apply_overlay_event resets the focus debouncer on entry to closed and
    # reconciles the transient destroy hook. None of the PAINT_ACK paths here
    # enter closed or touch the hook, but inject the fields it may read so the
    # method body never raises an AttributeError on the shared seam.
    controller._overlay_focus_debouncer = MagicMock()
    controller._overlay_focus_hooks = None
    controller._overlay_destroy_hook_active = False
    controller._overlay_tracked_identity = None
    return controller


def _effects_mock(controller) -> MagicMock:
    """Return the ``_perform_overlay_effects`` MagicMock as a typed handle.

    The helper assigns a ``MagicMock`` over the bound method; pyright still sees
    the declared ``MethodType``, so cast at the assertion site to keep the test
    file pyright-clean while asserting on the mock's call records.
    """

    return cast(MagicMock, controller._perform_overlay_effects)


def _drive_to_paint_in_flight(controller) -> tuple[int, int]:
    """Drive the machine into ``paint_in_flight`` and return its active pair.

    A PAINT_ACK is only non-stale (and only drives a transition) when the
    machine is expecting one. SHOW_NUMBERS moves closed -> walk_in_flight at a
    fresh ``(overlay_session_id, paint_generation)``; a matching BUILD_RESPONSE
    moves walk_in_flight -> paint_in_flight, where a PAINTED ack drives ->
    painted and a FAILED ack drives -> closed. Returns the active pair so the
    test can build a wire dict with matching values.
    """

    machine = controller.click_overlay_state
    machine.apply(OverlayEvent(kind=OverlayEventKind.SHOW_NUMBERS))
    sid = machine.overlay_session_id
    gen = machine.paint_generation
    machine.apply(
        OverlayEvent(
            kind=OverlayEventKind.BUILD_RESPONSE,
            overlay_session_id=sid,
            paint_generation=gen,
            snapshot_id="snap-1",
            build_ok=True,
        )
    )
    assert machine.state is OverlayState.PAINT_IN_FLIGHT
    return sid, gen


def _wire(
    state: str,
    overlay_session_id: int,
    paint_generation: int,
    *,
    snapshot_id="snap-1",
    monitor_ids=(0,),
):
    """Build a valid overlay_state_changed wire dict (via the schema to_dict)."""

    return OverlayStateChangedEvent(
        state=state,
        overlay_session_id=overlay_session_id,
        paint_generation=paint_generation,
        monitor_ids=tuple(monitor_ids),
        snapshot_id=snapshot_id,
    ).to_dict()


# ---------------------------------------------------------------------------
# (a) painted ack at the active pair drives the transition + hands off effects.
# ---------------------------------------------------------------------------


def test_painted_ack_at_active_pair_drives_painted_and_hands_off_effects():
    controller = _controller()
    sid, gen = _drive_to_paint_in_flight(controller)

    asyncio.run(
        controller._handle_overlay_state_changed(_wire("painted", sid, gen))
    )

    # paint_in_flight + painted ack -> painted (the machine's contract).
    assert controller.click_overlay_state.state is OverlayState.PAINTED
    # _apply_overlay_event handed the returned effects (CANCEL_TIMER) to the
    # effects seam. The painted-ack transition returns a non-empty effect tuple,
    # so the seam was called exactly once.
    assert _effects_mock(controller).call_count == 1


# ---------------------------------------------------------------------------
# (b) failed ack maps to PaintAckState.FAILED and is applied.
# ---------------------------------------------------------------------------


def test_failed_ack_maps_to_failed_and_is_applied():
    controller = _controller()
    sid, gen = _drive_to_paint_in_flight(controller)

    asyncio.run(
        controller._handle_overlay_state_changed(_wire("failed", sid, gen))
    )

    # paint_in_flight + failed ack -> error -> closed (the machine recovers to
    # closed). A failed ack must NOT leave the machine in painted.
    assert controller.click_overlay_state.state is OverlayState.CLOSED
    # The failure recovery returns effects (cancel_timer, unpin, ...), handed off.
    assert _effects_mock(controller).call_count == 1


# ---------------------------------------------------------------------------
# (c) cleared ack maps to PaintAckState.CLEARED and is a bookkeeping no-op.
# ---------------------------------------------------------------------------


def test_cleared_ack_maps_to_cleared_and_is_bookkeeping_no_op():
    controller = _controller()
    sid, gen = _drive_to_paint_in_flight(controller)

    # A cleared ack in paint_in_flight is bookkeeping only (NO_OP): no state
    # change, no effects. It must not raise and must not corrupt state.
    asyncio.run(
        controller._handle_overlay_state_changed(_wire("cleared", sid, gen))
    )

    assert controller.click_overlay_state.state is OverlayState.PAINT_IN_FLIGHT
    # NO_OP returns no effects, so the seam is never called.
    _effects_mock(controller).assert_not_called()


# ---------------------------------------------------------------------------
# (d) GENERATION GATE: a non-matching generation -> STALE_GENERATION, no change.
# ---------------------------------------------------------------------------


def test_stale_generation_is_dropped_by_machine_no_change_no_effects():
    controller = _controller()
    sid, gen = _drive_to_paint_in_flight(controller)

    # A paint_generation that does not match the active pair: the machine
    # returns STALE_GENERATION with NO state change and NO effects, BEFORE the
    # transition table. The handler must pass the wire pair through and NOT
    # re-implement a check, so a stale ack leaves the machine in paint_in_flight.
    asyncio.run(
        controller._handle_overlay_state_changed(
            _wire("painted", sid, gen + 7)
        )
    )

    assert controller.click_overlay_state.state is OverlayState.PAINT_IN_FLIGHT
    _effects_mock(controller).assert_not_called()


def test_stale_session_is_dropped_by_machine_no_change_no_effects():
    controller = _controller()
    sid, gen = _drive_to_paint_in_flight(controller)

    asyncio.run(
        controller._handle_overlay_state_changed(
            _wire("painted", sid + 99, gen)
        )
    )

    assert controller.click_overlay_state.state is OverlayState.PAINT_IN_FLIGHT
    _effects_mock(controller).assert_not_called()


# ---------------------------------------------------------------------------
# (e) malformed payload is dropped via safe_parse without raising.
# ---------------------------------------------------------------------------


def test_malformed_payload_missing_field_dropped_without_raising():
    controller = _controller()
    sid, gen = _drive_to_paint_in_flight(controller)

    # Missing the required 'snapshot_id' field -> OverlayStateChangedEventSchemaError
    # (a ValueError) which safe_parse logs and drops. The handler must return
    # without raising and without touching the machine.
    bad = {
        "action": ACTION_NAME,
        "state": "painted",
        "overlay_session_id": sid,
        "paint_generation": gen,
        "monitor_ids": [0],
        # snapshot_id intentionally omitted
    }
    asyncio.run(controller._handle_overlay_state_changed(bad))

    assert controller.click_overlay_state.state is OverlayState.PAINT_IN_FLIGHT
    _effects_mock(controller).assert_not_called()


def test_wrong_action_payload_dropped_without_raising():
    controller = _controller()
    sid, gen = _drive_to_paint_in_flight(controller)

    bad = _wire("painted", sid, gen)
    bad["action"] = "not_overlay_state_changed"
    asyncio.run(controller._handle_overlay_state_changed(bad))

    assert controller.click_overlay_state.state is OverlayState.PAINT_IN_FLIGHT
    _effects_mock(controller).assert_not_called()


# ---------------------------------------------------------------------------
# (f) overlay inactive -> handler early-returns, never touches the machine.
# ---------------------------------------------------------------------------


def test_inactive_disabled_early_returns_without_touching_machine():
    controller = _controller(enabled=False, overlay_effective=True)
    sid, gen = _drive_to_paint_in_flight(controller)

    asyncio.run(
        controller._handle_overlay_state_changed(_wire("painted", sid, gen))
    )

    assert controller.click_overlay_state.state is OverlayState.PAINT_IN_FLIGHT
    _effects_mock(controller).assert_not_called()


def test_inactive_overlay_not_effective_early_returns_without_touching_machine():
    controller = _controller(enabled=True, overlay_effective=False)
    sid, gen = _drive_to_paint_in_flight(controller)

    asyncio.run(
        controller._handle_overlay_state_changed(_wire("painted", sid, gen))
    )

    assert controller.click_overlay_state.state is OverlayState.PAINT_IN_FLIGHT
    _effects_mock(controller).assert_not_called()


# ---------------------------------------------------------------------------
# (g) _build_gui_handler_map binds 'overlay_state_changed' to a callable.
# ---------------------------------------------------------------------------


def test_handler_map_binds_overlay_state_changed_to_a_callable():
    # _build_gui_handler_map builds the FULL action->callable map. Most entries
    # are deferred lambdas, but a few values are dereferenced AT BUILD TIME
    # (e.g. self.state_manager.send_state_update). Build on a bare controller
    # with state_manager stubbed so the map constructs, then invoke ONLY the
    # overlay_state_changed entry. This catches a copy-paste mis-binding (wrong
    # handler on the right key) without exercising the unrelated entries.
    controller = object.__new__(LogicController)
    controller.state_manager = MagicMock()

    # Capture the coroutine the overlay lambda schedules and assert it targets
    # the handler. Record the coroutine passed and close it to avoid an
    # un-awaited-coroutine warning.
    captured: dict = {}

    def _capture(coro, task_name):
        captured["task_name"] = task_name
        captured["coro_name"] = getattr(
            getattr(coro, "cr_code", None), "co_name", None
        )
        coro.close()
        return MagicMock()

    controller.create_task_with_error_handling = _capture  # type: ignore[method-assign]

    command = _wire("painted", 1, 0)
    handler_map = controller._build_gui_handler_map(command)

    assert "overlay_state_changed" in handler_map
    entry = handler_map["overlay_state_changed"]
    assert callable(entry)
    entry()  # invoke the lambda -> calls create_task_with_error_handling
    assert captured.get("coro_name") == "_handle_overlay_state_changed"
    assert captured.get("task_name") == "OverlayStateChanged"


# ---------------------------------------------------------------------------
# (h) DEGRADE, DO NOT DIE: an unexpected error in the post-parse body is
#     logged and dropped, never propagated (wh-n29v.68.1).
# ---------------------------------------------------------------------------


def test_unexpected_error_in_apply_is_swallowed_not_escalated():
    """An unexpected error in the post-parse body must be logged and dropped,
    NOT propagated out of the coroutine.

    The handler runs as a ``create_task_with_error_handling`` background task
    whose done-callback (``_handle_task_completion``) calls ``request_shutdown()``
    on ANY uncaught exception. An unguarded raise here would therefore restart
    the whole Logic process -- defeating wh-uf54's "version-skewed sender cannot
    crash the listener" intent and diverging from the ``_handle_snapshot_item_clicked``
    degrade-don't-die precedent in the same file. ``safe_parse`` only covers the
    malformed-payload (ValueError) path; the ``PaintAckState`` mapping and the
    ``_apply_overlay_event`` hand-off (whose effects seam becomes real cross-process
    IPC in the integration slice) are the realistic unexpected-error sources.
    """
    controller = _controller()
    sid, gen = _drive_to_paint_in_flight(controller)

    # Replace the apply hand-off with one that raises an unexpected error,
    # standing in for any failure in the effects path or an enum/schema drift.
    controller._apply_overlay_event = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom")
    )

    # Must NOT raise -- asyncio.run re-raises an exception that escapes the
    # coroutine, so a propagating error fails this test (and, in production,
    # restarts Logic).
    asyncio.run(
        controller._handle_overlay_state_changed(_wire("painted", sid, gen))
    )

    # The guard wraps the real work, not an early return: the body reached the
    # apply call before the error was swallowed.
    cast(MagicMock, controller._apply_overlay_event).assert_called_once()


# ---------------------------------------------------------------------------
# (i) painted ack while auto_hide -> PAUSED, and _apply_overlay_event reconciles
#     the transient destroy hook to match (wh-n29v.68.2).
# ---------------------------------------------------------------------------


def test_painted_ack_while_auto_hide_drives_paused_and_registers_destroy_hook():
    """A painted ack while ``auto_hide_in_flight`` drives paint_in_flight ->
    paused, and ``_apply_overlay_event`` then registers the transient destroy
    hook (scoped to the tracked window's pid/tid) to match the paused state.

    The other tests set ``_overlay_focus_hooks=None`` so the hook-registration
    branch in ``_reconcile_overlay_destroy_hook`` never runs; this test primes a
    hook manager, a tracked identity, and a pid/tid resolver so the
    paint-ack -> PAUSED -> destroy-hook-register reuse this handler triggers is
    actually exercised.
    """
    controller = _controller()
    sid, gen = _drive_to_paint_in_flight(controller)

    # Prime the transient-destroy-hook dependencies the default helper nulls out.
    manager = MagicMock()
    manager.register_destroy_hook.return_value = True
    controller._overlay_focus_hooks = manager
    tracked = MagicMock()
    tracked.hwnd = 4321
    controller._overlay_tracked_identity = tracked
    controller._overlay_window_pid_tid = MagicMock(  # type: ignore[method-assign]
        return_value=(111, 222)
    )

    # MIC_PAUSE in paint_in_flight sets auto_hide_in_flight and stays
    # paint_in_flight, so the next painted ack routes to _paint_ack_to_paused.
    controller.click_overlay_state.apply(
        OverlayEvent(kind=OverlayEventKind.MIC_PAUSE)
    )
    assert controller.click_overlay_state.auto_hide_in_flight is True

    asyncio.run(
        controller._handle_overlay_state_changed(_wire("painted", sid, gen))
    )

    assert controller.click_overlay_state.state is OverlayState.PAUSED
    manager.register_destroy_hook.assert_called_once_with(pid=111, tid=222)
    assert controller._overlay_destroy_hook_active is True
    # The paint-ack -> paused transition returns effects, handed to the seam.
    assert _effects_mock(controller).call_count == 1


# ---------------------------------------------------------------------------
# (j) a generation-matching ack arriving AFTER the machine has entered paused
#     is bookkeeping, not an error (wh-n29v.69.1).
# ---------------------------------------------------------------------------


def test_cleared_ack_in_paused_is_bookkeeping_not_error():
    """The clear that hides the overlay when the mic pauses produces a
    generation-matching 'cleared' ack AFTER the machine has entered paused.

    The Logic consumer now delivers that ack to the machine (before this slice
    it was dropped as an unknown GUI action, so paused never saw it). Paused
    must treat a generation-matching paint-ack as bookkeeping (NO_OP), the same
    way closed does, NOT corrupt to ERROR via the invalid-transition path
    (wh-n29v.69.1). This is reachable on a common flow: pause dictation while
    numbers are visible.
    """
    controller = _controller()
    sid, gen = _drive_to_paint_in_flight(controller)

    # MIC_PAUSE arms auto-hide; the painted ack then drives paint_in_flight ->
    # paused (and dispatches the clear the GUI will acknowledge).
    controller.click_overlay_state.apply(
        OverlayEvent(kind=OverlayEventKind.MIC_PAUSE)
    )
    asyncio.run(
        controller._handle_overlay_state_changed(_wire("painted", sid, gen))
    )
    assert controller.click_overlay_state.state is OverlayState.PAUSED

    # The 'cleared' ack for that hide arrives at the SAME generation.
    asyncio.run(
        controller._handle_overlay_state_changed(_wire("cleared", sid, gen))
    )
    assert controller.click_overlay_state.state is OverlayState.PAUSED


def test_painted_ack_in_paused_is_bookkeeping_not_error():
    """A 'painted' ack can also arrive in paused: the in-flight->paused resolve
    dispatches a paint with immediate-clear, so the GUI emits a painted ack for
    an already-hidden overlay. Paused must consume it as bookkeeping, not error.
    """
    controller = _controller()
    sid, gen = _drive_to_paint_in_flight(controller)
    controller.click_overlay_state.apply(
        OverlayEvent(kind=OverlayEventKind.MIC_PAUSE)
    )
    asyncio.run(
        controller._handle_overlay_state_changed(_wire("painted", sid, gen))
    )
    assert controller.click_overlay_state.state is OverlayState.PAUSED

    # A duplicate painted ack at the same generation in paused: bookkeeping.
    asyncio.run(
        controller._handle_overlay_state_changed(_wire("painted", sid, gen))
    )
    assert controller.click_overlay_state.state is OverlayState.PAUSED


# ---------------------------------------------------------------------------
# (k) completed fire-and-forget tasks are not retained (wh-n29v.69.2).
# ---------------------------------------------------------------------------


def test_completed_overlay_task_is_discarded_from_background_tasks():
    """create_task_with_error_handling must not retain completed tasks.

    overlay_state_changed is emitted on every overlay paint / failed-paint /
    clear, so a task retained per ack would accumulate for the lifetime of the
    Logic process and bloat the shutdown gather (wh-n29v.69.2). The done-callback
    discards the finished task from self.background_tasks.
    """
    controller = object.__new__(LogicController)
    controller.background_tasks = []

    async def _run():
        controller.loop = asyncio.get_running_loop()

        async def _noop():
            return None

        task = controller.create_task_with_error_handling(
            _noop(), "OverlayStateChanged"
        )
        await task
        # The done-callback is scheduled via call_soon; yield so it runs.
        await asyncio.sleep(0)
        return len(controller.background_tasks)

    assert asyncio.run(_run()) == 0
