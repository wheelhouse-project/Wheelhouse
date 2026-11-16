"""MouseGridController: the mouse grid and the pointer click (area L5).

wh-full-refactor.6.2 (stage B of place 3) moved this code out of
LogicController in main.py without a change in behavior. The stage A boundary
tests (tests/test_mouse_grid_requirements_l5.py,
docs/design/mouse-grid-requirements.md, rows MG-n) pin that behavior.
LogicController creates one instance as self.mouse_grid and calls in through
it; this class reads or calls the LogicController members it does not own
through self._host (see host.py). It runs in the Logic process.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from services.wheelhouse.mouse_grid.host import MouseGridHost

# The logger of main.py, by name. The Logic process imports main.py as module
# "main" (launcher.py: from main import start_logic_process), so the moved
# code logs under the same logger name as before the move.
logger = logging.getLogger("main")


class MouseGridController:
    """The mouse grid commands and the pointer actions of one LogicController.

    It owns the grid pointer lock (_grid_mouse_action_lock) and the set of
    pending pointer tasks (_pending_grid_mouse_actions), which the methods
    below create on first use, and the two flags of the session fence
    (_grid_session_transition, _grid_mouse_channel_suspect).
    """

    def __init__(self, host: "MouseGridHost") -> None:
        """``host`` is the LogicController that owns this object."""
        self._host = host
        # wh-mouse-grid.1.32: True while a grid open / next_screen is
        # between its quiescence fence and its paint (handle_grid_command
        # sets and clears it). The numbered overlay reads it through
        # LogicController.grid_session_in_transition.
        self._grid_session_transition = False
        # wh-mouse-grid.1.18: True after a pointer send to Input ended
        # without an answer. While it is True, the next pointer action and
        # grid_pointer_channel_quiescent send a channel_probe first.
        self._grid_mouse_channel_suspect = False

    def grid_session_in_transition(self) -> bool:
        """True while a grid open / next_screen is between fence and paint."""
        return self._grid_session_transition

    def mark_grid_mouse_channel_suspect(self) -> None:
        """Record that a pointer send to Input ended without an answer."""
        self._grid_mouse_channel_suspect = True

    # ------------------------------------------------------------------
    # wh-grid-speech-routing: mouse-grid command integration.
    #
    # ``handle_grid_command`` is the single Logic entry point the speech
    # actions delegate to. It applies GridEvents to the pure
    # ``grid_overlay_state`` machine and PERFORMS the returned effects:
    # paint/pin/clear ride the GUI state queue via the shared/paint_grid*
    # + shared/clear_grid schemas, mouse actions ride ``send_request`` to
    # the Input-process primitives (click_point / move_pointer /
    # perform_drag), and notices reuse ``forward_click_notice`` with
    # grid-specific reason tags (wording in click_notice_toast_wording.py).
    # ------------------------------------------------------------------
    def _grid_monitor_context(self):
        """Enumerate monitors + resolve the focused window's monitor (seam).

        Returns ``(monitors, focused_monitor)`` as ``GridRect``s in
        virtual-desktop physical pixels -- the shapes ``GridEvent`` carries.
        Blocking Win32 seam: call it via ``asyncio.to_thread`` (see
        ``_grid_monitor_context_off_loop``). Never raises: enumeration
        failure returns ``((), None)`` (the machine degrades that to
        NO_MONITOR -> notice), and a focused-window resolution failure
        returns ``focused=None`` (the machine falls back to the primary
        monitor).

        Every User32 read happens inside ``per_monitor_dpi_context``:
        the Logic process is plain ``python.exe`` (DPI-UNAWARE -- no Qt,
        no uiautomation import), so without a per-monitor thread context
        the monitor and window rectangles come back DPI-virtualized and
        the grid would paint and click at the wrong physical locations
        on any scaled or mixed-DPI desktop. When the context cannot be
        established, this refuses (returns ``((), None)``) rather than
        ship virtualized coordinates (wh-mouse-grid.1.16).
        """
        from services.wheelhouse.grid_overlay_state import GridRect
        # Lazy import: monitor_geometry imports PySide6.QtCore for QRect.
        from services.wheelhouse.shared import monitor_geometry

        def _to_rect(native) -> GridRect:
            r = native.rect_phys
            return GridRect(r.left(), r.top(), r.width(), r.height())

        with monitor_geometry.per_monitor_dpi_context() as dpi_ok:
            if not dpi_ok:
                logger.warning(
                    "grid: no per-monitor DPI context on this thread; "
                    "monitor coordinates would be virtualized -- "
                    "refusing to open the grid"
                )
                return (), None
            try:
                native_monitors = (
                    monitor_geometry._enumerate_native_monitors()
                )
            except Exception as exc:  # noqa: BLE001 -- NO_MONITOR degrade
                logger.warning("grid: monitor enumeration failed: %s", exc)
                return (), None
            monitors = tuple(_to_rect(m) for m in native_monitors)
            focused = None
            try:
                import win32gui

                hwnd = win32gui.GetForegroundWindow()
                if hwnd:
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                    target = monitor_geometry._resolve_target_monitor(
                        (left, top, right, bottom), native_monitors,
                    )
                    if target is not None:
                        focused = _to_rect(target)
            except Exception as exc:  # noqa: BLE001 -- primary fallback
                logger.debug(
                    "grid: focused-monitor resolution failed: %s", exc
                )
                focused = None
            return monitors, focused

    async def _grid_monitor_context_off_loop(self):
        """Run the blocking monitor seam off the Logic loop; never raises."""
        try:
            return await asyncio.to_thread(self._grid_monitor_context)
        except Exception as exc:  # noqa: BLE001 -- degrade to NO_MONITOR
            logger.warning(
                "grid: monitor context off-loop call failed: %s", exc,
            )
            return (), None

    def _grid_gesture_buttons(self, gesture) -> "tuple[str, int]":
        """Map a ClickGesture to the (button, click_count) a grid click sends.

        Grid clicks are always PHYSICAL coordinate clicks (the design doc's
        "no UIA verification" rule), so INVOKE means a plain left click.
        The pointer click (``handle_pointer_click``) uses the same map.
        ``ClickGesture`` subclasses str, so == also accepts the bare wire
        string.
        """
        from services.wheelhouse.ui.element_types import ClickGesture

        if gesture == ClickGesture.RIGHT_CLICK:
            return ("right", 1)
        if gesture == ClickGesture.DOUBLE_CLICK:
            return ("left", 2)
        if gesture == ClickGesture.TRIPLE_CLICK:
            # wh-voice-access-parity.2.5 (R4): without this entry a triple
            # click would fall through to a silent single click.
            return ("left", 3)
        return ("left", 1)

    async def handle_pointer_click(
        self, gesture, trace_id: str, *, spoken: str,
    ) -> bool:
        """Click at the current pointer position (wh-voice-access-parity.2.5).

        The speech actions call this when a bare click word ("click",
        "tap", "right click", "double click", "triple click") was not
        taken by the mouse grid, which means the grid is closed. It sends
        the Input action ``click_at_pointer`` with the gesture's button and
        count through ``_dispatch_mouse_action``, so the click shares the
        grid's pointer lock, its suspect-channel probe, and the session
        fence (``_pending_grid_mouse_actions``). Input reads the pointer
        position itself: Logic is DPI-unaware and would read a virtualized
        position on a scaled monitor.

        Returns True when the click was dispatched. Returns False when the
        ``[click] enabled`` master switch is off; the caller then dictates
        the words, which is the behaviour before this bead. A failed click
        shows the ``pointer_click_failed`` notice.
        """
        from utils.trace_context import set_trace

        set_trace(trace_id)
        if not self._host.click_config.enabled:
            logger.info(
                "pointer click: voice clicking disabled by config; the "
                "words dictate (trace_id=%s)", trace_id,
            )
            return False
        button, click_count = self._grid_gesture_buttons(gesture)
        self._dispatch_mouse_action(
            "click_at_pointer",
            {"button": button, "click_count": click_count},
            trace_id=trace_id, spoken=spoken,
            failure_reason="pointer_click_failed",
        )
        logger.info(
            "pointer click: %s x%d at the pointer dispatched (trace_id=%s)",
            button, click_count, trace_id,
        )
        return True

    async def handle_grid_command(
        self, command: str, trace_id: str, *,
        number: int,
        gesture,
        spoken: str,
    ) -> bool:
        """Apply one spoken mouse-grid command (wh-grid-speech-routing).

        ``command`` is one of:

          * ``"open"`` / ``"dismiss"`` / ``"next_screen"`` -- the explicit
            grid commands ("show grid", "hide grid",
            "grid next screen"). Always CONSUMED (return True), even as a
            no-op with the grid closed -- the phrasing is unambiguous.
          * ``"refine"`` (with ``number`` 1..9), ``"mark"``, ``"drag"``,
            ``"move_here"``, ``"click"`` (optional ``number`` for the
            combined "click 5" shortcut, optional ``gesture``) -- the
            grid-OPEN-only words. With the grid closed they return False so
            the actions layer falls back to dictation (the words are
            ordinary dictation vocabulary).

        Returns True when the utterance was consumed as a grid command,
        False when the caller should dictate it. A number the grid consumes
        -- including a refine refused at IGNORED_MIN_CELL -- NEVER returns
        False. Config gate: with ``[click] enabled`` False the grid cannot
        open; ``open`` shows the same one-shot disabled_by_config notice as
        by-name clicking, and the grid-open-only words dictate.
        """
        from utils.trace_context import set_trace
        from services.wheelhouse.grid_overlay_state import (
            GridEvent,
            GridEventKind,
            GridOutcome,
        )

        set_trace(trace_id)
        machine = self._host.grid_overlay_state
        if machine is None:
            logger.warning(
                "grid: no grid_overlay_state; %r not consumed "
                "(trace_id=%s)", command, trace_id,
            )
            return False

        if command in ("open", "dismiss", "next_screen"):
            # grid_enabled_effective, not enabled: the grid's two keys
            # validate on the overlay track, so a bad grid_min_cell_px /
            # drag_duration_ms leaves enabled True while the grid must
            # refuse (wh-mouse-grid.1.21). A valid overlay_enabled=false
            # opt-out does NOT trip this gate.
            if not self._host.click_config.grid_enabled_effective:
                # Same one-shot disabled-by-config notice pattern (and the
                # same per-session flag) as forward_click_element's gate.
                if command == "open" and self._host.take_click_disabled_notice():
                    logger.info(
                        "grid: %r with the grid disabled by config "
                        "(invalid_key=%s, overlay_invalid_key=%s); showing "
                        "one-shot notice (trace_id=%s)",
                        command, self._host.click_config.invalid_key,
                        self._host.click_config.overlay_invalid_key, trace_id,
                    )
                    self._host.overlay.forward_click_notice(
                        outcome="execution_failed",
                        reason="disabled_by_config",
                        matched_name=None,
                        matched_names=(),
                        spoken_name=spoken,
                        snapshot_id=None,
                        trace_id=trace_id,
                    )
                else:
                    logger.debug(
                        "grid: %r ignored, voice clicking disabled by "
                        "config (trace_id=%s)", command, trace_id,
                    )
                return True

            if command == "dismiss":
                result = machine.apply(GridEvent(GridEventKind.DISMISS))
                await self._perform_grid_effects(
                    result.effects, trace_id=trace_id, spoken=spoken,
                )
                logger.info(
                    "grid: dismiss -> outcome=%s (trace_id=%s)",
                    result.outcome.value, trace_id,
                )
                return True

            if command == "next_screen" and not machine.is_open:
                logger.info(
                    "grid: next_screen with grid closed; no-op "
                    "(trace_id=%s)", trace_id,
                )
                return True

            # wh-mouse-grid.1.32: between here and the new grid's paint
            # this branch awaits (the monitor lookup, and overlay
            # teardown inside _perform_grid_effects); a badge click
            # dispatched by a concurrent task during those awaits (the
            # GUI badge-click event, the held-click resolver) would slip
            # behind the completed quiescence fence and execute under
            # the fresh grid. The overlay's badge-click dispatch
            # refuses dispatches while this flag is set: the click targets an
            # overlay this transition is tearing down.
            self._grid_session_transition = True
            try:
                # wh-mouse-grid.1.29: a dispatched click/move/drag may
                # still be queued or executing in Input; the new grid
                # must not become visible until it provably cannot act
                # (a multi-second drag could press and move under the
                # fresh grid). An unconfirmed channel withholds the
                # open, the same policy the next pointer action follows.
                if not await self.grid_pointer_channel_quiescent(
                    trace_id=trace_id,
                ):
                    logger.warning(
                        "grid: %r withheld, pointer channel unconfirmed "
                        "after an earlier timeout (trace_id=%s)",
                        command, trace_id,
                    )
                    self._host.overlay.forward_click_notice(
                        outcome="execution_failed",
                        reason="grid_pointer_pending",
                        matched_name=None,
                        matched_names=(),
                        spoken_name=spoken,
                        snapshot_id=None,
                        trace_id=trace_id,
                    )
                    return True

                monitors, focused = (
                    await self._grid_monitor_context_off_loop()
                )
                if command == "open":
                    event = GridEvent(
                        GridEventKind.OPEN_GRID,
                        monitors=monitors,
                        focused_monitor=focused,
                    )
                else:
                    event = GridEvent(
                        GridEventKind.NEXT_MONITOR, monitors=monitors,
                    )
                result = machine.apply(event)
                if result.outcome is GridOutcome.NO_MONITOR:
                    logger.warning(
                        "grid: %r found no usable monitor (trace_id=%s)",
                        command, trace_id,
                    )
                    self._host.overlay.forward_click_notice(
                        outcome="execution_failed",
                        reason="grid_no_monitor",
                        matched_name=None,
                        matched_names=(),
                        spoken_name=spoken,
                        snapshot_id=None,
                        trace_id=trace_id,
                    )
                    return True
                await self._perform_grid_effects(
                    result.effects, trace_id=trace_id, spoken=spoken,
                )
                logger.info(
                    "grid: %s -> outcome=%s (%d effect(s), trace_id=%s)",
                    command, result.outcome.value, len(result.effects),
                    trace_id,
                )
                return True
            finally:
                self._grid_session_transition = False

        # Grid-OPEN-only words. With the grid closed (including the
        # disabled-config case, where it can never open) these are ordinary
        # dictation words: not consumed.
        if not machine.is_open:
            return False

        if command == "refine":
            result = machine.apply(
                GridEvent(GridEventKind.REFINE, number=number)
            )
            if result.outcome is GridOutcome.NO_OP:
                # A number the machine would not accept (outside 1..9) is
                # not a grid utterance; let it dictate. IGNORED_MIN_CELL is
                # NOT this case: that number was consumed, silently.
                return False
            await self._perform_grid_effects(
                result.effects, trace_id=trace_id, spoken=spoken,
            )
            logger.info(
                "grid: refine %d -> outcome=%s (trace_id=%s)",
                number, result.outcome.value, trace_id,
            )
            return True

        if command == "mark":
            result = machine.apply(GridEvent(GridEventKind.MARK))
            await self._perform_grid_effects(
                result.effects, trace_id=trace_id, spoken=spoken,
            )
            logger.info(
                "grid: mark -> outcome=%s (trace_id=%s)",
                result.outcome.value, trace_id,
            )
            return True

        if command == "drag":
            result = machine.apply(GridEvent(GridEventKind.DRAG_REQUESTED))
            # NO_MARK returns a FIRE_NOTICE effect; ACCEPTED returns
            # PERFORM_DRAG + CLEAR_GRID. Both are performed uniformly.
            await self._perform_grid_effects(
                result.effects, trace_id=trace_id, spoken=spoken,
            )
            logger.info(
                "grid: drag -> outcome=%s (trace_id=%s)",
                result.outcome.value, trace_id,
            )
            return True

        if command in ("click", "move_here"):
            if command == "click" and number:
                refine_result = machine.apply(
                    GridEvent(GridEventKind.REFINE, number=number)
                )
                # Combined "click 5": the refine's paint effects are
                # deliberately NOT performed -- the grid closes in this same
                # utterance, so painting the refined cell would only flash
                # for one frame. An IGNORED_MIN_CELL refine still proceeds
                # to the click ("click 5" at minimum depth means exactly
                # "5 then click"): the click lands at the unchanged center.
                logger.info(
                    "grid: click %d refine -> outcome=%s (trace_id=%s)",
                    number, refine_result.outcome.value, trace_id,
                )
            center = machine.current_center
            if center is None:
                # Defensive: an open grid always has a center.
                logger.warning(
                    "grid: %r with no current center; not consumed "
                    "(trace_id=%s)", command, trace_id,
                )
                return False
            result = machine.apply(GridEvent(GridEventKind.ACTION_COMPLETED))
            await self._perform_grid_effects(
                result.effects, trace_id=trace_id, spoken=spoken,
            )
            if command == "move_here":
                self._dispatch_mouse_action(
                    "move_pointer",
                    {"x": center.x, "y": center.y},
                    trace_id=trace_id, spoken=spoken,
                    failure_reason="grid_move_failed",
                )
            else:
                button, click_count = self._grid_gesture_buttons(gesture)
                self._dispatch_mouse_action(
                    "click_point",
                    {
                        "x": center.x, "y": center.y,
                        "button": button, "click_count": click_count,
                    },
                    trace_id=trace_id, spoken=spoken,
                    failure_reason="grid_click_failed",
                )
            logger.info(
                "grid: %s at (%d, %d) dispatched; grid closed "
                "(trace_id=%s)", command, center.x, center.y, trace_id,
            )
            return True

        logger.warning(
            "grid: unknown command %r ignored (trace_id=%s)",
            command, trace_id,
        )
        return True

    async def _perform_grid_effects(
        self, effects, *, trace_id: str, spoken: str = "",
    ) -> None:
        """Perform the machine's ordered grid effects (wh-grid-speech-routing).

        Paint/pin/clear go to the GUI state queue as the shared schema
        dicts (each ``to_dict`` carries its own ``action`` key);
        CLOSE_NUMBERED_OVERLAY reuses ``handle_overlay_command('hide')``
        (idempotent, and it runs every overlay reconcile helper);
        PERFORM_DRAG dispatches the Input round trip as a background task;
        FIRE_NOTICE reuses the click-notice path. Never raises: a
        malformed effect logs and is skipped so the remaining effects
        still run.
        """
        from services.wheelhouse.grid_overlay_state import GridEffectKind

        for effect in effects:
            try:
                if effect.kind is GridEffectKind.PAINT_GRID:
                    from shared.paint_grid import PaintGridEvent

                    monitor, rect = effect.monitor, effect.rect
                    if monitor is None or rect is None:
                        logger.warning(
                            "grid: PAINT_GRID effect missing monitor/rect; "
                            "skipped (trace_id=%s)", trace_id,
                        )
                        continue
                    payload = PaintGridEvent(
                        monitor_left=monitor.left,
                        monitor_top=monitor.top,
                        monitor_width=monitor.width,
                        monitor_height=monitor.height,
                        left=rect.left,
                        top=rect.top,
                        width=rect.width,
                        height=rect.height,
                    ).to_dict()
                    self.put_grid_gui_event(payload, trace_id)
                elif effect.kind is GridEffectKind.PAINT_PIN:
                    from shared.paint_grid_pin import PaintGridPinEvent

                    point = effect.point
                    if point is None:
                        logger.warning(
                            "grid: PAINT_PIN effect missing point; skipped "
                            "(trace_id=%s)", trace_id,
                        )
                        continue
                    self.put_grid_gui_event(
                        PaintGridPinEvent(x=point.x, y=point.y).to_dict(),
                        trace_id,
                    )
                elif effect.kind is GridEffectKind.CLEAR_GRID:
                    from shared.clear_grid import ClearGridEvent

                    self.put_grid_gui_event(
                        ClearGridEvent().to_dict(), trace_id,
                    )
                elif effect.kind is GridEffectKind.CLOSE_NUMBERED_OVERLAY:
                    # wh-mouse-grid.1.17: handle_overlay_command only
                    # SCHEDULES its clear batch (behind the overlay
                    # controller's _overlay_effect_lock);
                    # await the batch task so a later PAINT_GRID in this
                    # effect list cannot reach the GUI queue before the
                    # numbered-overlay clear has shipped. Deadlock-free: the
                    # await direction is grid -> overlay only (the overlay
                    # dispatch never awaits grid work).
                    overlay_batch = await self._host.overlay.handle_overlay_command(
                        "hide", trace_id,
                    )
                    if overlay_batch is not None:
                        await overlay_batch
                elif effect.kind is GridEffectKind.PERFORM_DRAG:
                    start, end = effect.start_point, effect.end_point
                    if start is None or end is None:
                        logger.warning(
                            "grid: PERFORM_DRAG effect missing endpoints; "
                            "skipped (trace_id=%s)", trace_id,
                        )
                        continue
                    self._dispatch_mouse_action(
                        "perform_drag",
                        {
                            "start_x": start.x, "start_y": start.y,
                            "end_x": end.x, "end_y": end.y,
                            "duration_ms": effect.duration_ms,
                        },
                        trace_id=trace_id, spoken=spoken,
                        failure_reason="grid_drag_failed",
                    )
                elif effect.kind is GridEffectKind.FIRE_NOTICE:
                    self._host.overlay.forward_click_notice(
                        outcome="execution_failed",
                        reason=effect.notice_reason,
                        matched_name=None,
                        matched_names=(),
                        spoken_name=spoken,
                        snapshot_id=None,
                        trace_id=trace_id,
                    )
                else:
                    logger.warning(
                        "grid: unknown effect kind %r skipped "
                        "(trace_id=%s)", effect.kind, trace_id,
                    )
            except Exception as exc:  # noqa: BLE001 -- keep performing
                logger.warning(
                    "grid: effect %r failed (trace_id=%s): %s",
                    getattr(effect, "kind", None), trace_id, exc,
                    exc_info=True,
                )

    def put_grid_gui_event(self, payload: dict, trace_id: str) -> None:
        """Put one grid paint payload on the GUI state queue; never raises."""
        if not self._host.state_manager:
            logger.warning(
                "grid: GUI event %r dropped, no state_manager queue "
                "(trace_id=%s)", payload.get("action"), trace_id,
            )
            return
        try:
            self._host.state_manager.state_to_gui_queue.put_nowait(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "grid: GUI event %r dropped, queue put failed "
                "(trace_id=%s): %s", payload.get("action"), trace_id, exc,
            )


    def _dispatch_mouse_action(
        self, action: str, params: dict, *, trace_id: str, spoken: str,
        failure_reason: str,
    ) -> None:
        """Schedule a grid mouse primitive round trip as a background task.

        Mirrors ``_dispatch_snapshot_item_click``: the synchronous caller
        (``handle_grid_command`` after a state transition) must not block
        on the cross-process send. ``_send_mouse_action`` owns the await,
        the MouseActionResponse parse, and the failure notice.

        wh-mouse-grid.1.29: each task is recorded in
        ``_pending_grid_mouse_actions`` so a later session-visibility
        change (grid reopen, numbered-overlay show, ambiguous auto-open)
        can wait for it via ``grid_pointer_channel_quiescent`` -- a
        stale click/move/drag must not act under a fresh session's
        visual state.
        """
        task = self._host.create_task_with_error_handling(
            self._send_mouse_action(
                action, params, trace_id=trace_id, spoken=spoken,
                failure_reason=failure_reason,
            ),
            "GridMouseAction",
        )
        self.track_pending_pointer_task(task)

    def track_pending_pointer_task(self, task) -> None:
        """Record a pointer-capable background send for the session fence.

        wh-mouse-grid.1.29 / .1.30: every Logic-dispatched background
        task that can end in real pointer input -- grid click / move /
        drag AND numbered-badge clicks -- is recorded in
        ``_pending_grid_mouse_actions`` so
        ``grid_pointer_channel_quiescent`` can await it before a new
        session becomes visible. The done callback removes the task the
        moment it resolves, so the set only ever holds live work.
        """
        pending = getattr(self, "_pending_grid_mouse_actions", None)
        if pending is None:
            pending = set()
            self._pending_grid_mouse_actions = pending
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def _send_mouse_action(
        self, action: str, params: dict, *, trace_id: str, spoken: str,
        failure_reason: str,
    ) -> None:
        """Send one mouse primitive to Input and notice on failure.

        Mirrors the ``click_element`` awaiter's degrade paths: a timeout,
        a non-timeout send failure, a malformed reply, and a non-ok
        outcome each surface an ``execution_failed`` notice under
        ``failure_reason`` (grid_click_failed / grid_move_failed /
        grid_drag_failed, or pointer_click_failed for the no-grid pointer
        click) -- with two exceptions carrying their own tags:

          * ``release_failed`` -> ``grid_button_release_failed``: the ONE
            Input reason meaning the mouse button may still be held --
            from a failed drag release or a partial click batch whose
            compensating release was refused (wh-mouse-grid.1.5) -- so
            its wording must say so.
          * ``disabled_by_config`` -> forwarded as-is (the Input side
            independently validated its [click] config; the existing
            wording already points at config.toml).

        Never raises. No notice on ok (matching by-name clicks).
        """
        from utils.trace_context import set_trace

        set_trace(trace_id)
        timeout_ms = self._host.click_config.response_timeout_ms
        timeout_s = max(timeout_ms / 1000.0, 0.1)
        duration_ms = params.get("duration_ms")
        if isinstance(duration_ms, int) and not isinstance(duration_ms, bool):
            # perform_drag blocks in the Input handler for its interpolated
            # movement duration; the reply cannot arrive sooner.
            timeout_s += max(duration_ms, 0) / 1000.0
        request = dict(params)
        request["trace_id"] = trace_id

        def _notice(reason: str) -> None:
            self._host.overlay.forward_click_notice(
                outcome="execution_failed",
                reason=reason,
                matched_name=None,
                matched_names=(),
                spoken_name=spoken,
                snapshot_id=None,
                trace_id=trace_id,
            )

        # Serialize grid pointer actions through completion
        # (wh-mouse-grid.1.15): perform_drag blocks the synchronous Input
        # command loop for its whole interpolated duration, and a second
        # action sent during that window could time out at Logic (its
        # budget starts at enqueue), be reported failed, and still
        # execute later -- or overwrite an unread shared-memory payload
        # and be silently dropped. Admission through this lock means each
        # send_request's timeout starts at its own admission and at most
        # one grid pointer payload is ever outstanding. Created lazily on
        # the Logic loop; no await between the getattr and the setattr,
        # so two tasks cannot race the creation.
        lock = getattr(self, "_grid_mouse_action_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._grid_mouse_action_lock = lock
        try:
            async with lock:
                # wh-mouse-grid.1.18: a timed-out (or failed) send leaves the
                # channel SUSPECT -- the payload may still be queued in
                # app._outbound_q or executing inside Input, and could act on
                # the desktop after the user was told it failed. Before
                # admitting the next action, confirm the channel with a probe
                # round trip: Input's command loop is single-threaded and
                # in-order, so a probe reply proves everything queued before
                # it either executed or was overwritten and can never execute.
                if self._grid_mouse_channel_suspect:
                    if await self._confirm_grid_mouse_channel(
                        timeout_s=timeout_s, trace_id=trace_id,
                    ) is None:
                        logger.warning(
                            "grid: %s withheld, channel unconfirmed after an "
                            "earlier timeout; %s (trace_id=%s)",
                            action, failure_reason, trace_id,
                        )
                        _notice(failure_reason)
                        return
                try:
                    raw = await self._host.app.send_request(
                        action, params=request, timeout_s=timeout_s,
                    )
                except asyncio.TimeoutError:
                    # wh-mouse-grid.1.24: the timed-out action's own reply is
                    # discarded by the app demuxer, so probe NOW -- still
                    # under the lock, so no competing action interleaves --
                    # and read Input's record of the last emitted result. A
                    # trace_id match recovers what the discarded reply would
                    # have said: release_failed -> the button-specific
                    # stuck-button warning; success -> the action landed
                    # late, so a failure notice would be false. Anything
                    # else (no reply, a stale record, another reason) keeps
                    # the generic failure notice.
                    self._grid_mouse_channel_suspect = True
                    logger.warning(
                        "grid: %s no reply within %.1fs; %s (trace_id=%s)",
                        action, timeout_s, failure_reason, trace_id,
                    )
                    reply = await self._confirm_grid_mouse_channel(
                        timeout_s=timeout_s, trace_id=trace_id,
                    )
                    late = (
                        reply.get("last_mouse_action")
                        if isinstance(reply, dict) else None
                    )
                    if (
                        isinstance(late, dict)
                        and late.get("trace_id") == trace_id
                    ):
                        if late.get("succeeded") is True:
                            logger.info(
                                "grid: %s executed late; suppressing the "
                                "failure notice (trace_id=%s)",
                                action, trace_id,
                            )
                            return
                        if late.get("reason") == "release_failed":
                            if request.get("button") == "right":
                                _notice("grid_right_button_release_failed")
                            else:
                                _notice("grid_button_release_failed")
                            return
                    _notice(failure_reason)
                    return
                except BaseException:
                    self._grid_mouse_channel_suspect = True
                    raise
        except asyncio.TimeoutError:
            # WARNING, not ERROR: send_request already logged the fault at
            # ERROR (wh-duplicate-error-popup).
            logger.warning(
                "grid: %s no reply within %.1fs; %s (trace_id=%s)",
                action, timeout_s, failure_reason, trace_id,
            )
            _notice(failure_reason)
            return
        except Exception as exc:  # noqa: BLE001 -- never crash the task
            logger.warning(
                "grid: %s send_request failed (trace_id=%s): %s",
                action, trace_id, exc, exc_info=True,
            )
            _notice(failure_reason)
            return

        from shared.mouse_action import (
            MouseActionResponse,
            MouseActionResponseSchemaError,
        )

        try:
            response = MouseActionResponse.from_dict(raw)
        except (
            MouseActionResponseSchemaError, ValueError, KeyError, TypeError,
        ) as exc:
            logger.error(
                "grid: %s malformed response (trace_id=%s): %s",
                action, trace_id, exc,
            )
            _notice(failure_reason)
            return

        if response.outcome == "ok":
            logger.info(
                "grid: %s ok (trace_id=%s)", action, trace_id,
            )
            return

        reason = response.reason
        logger.warning(
            "grid: %s failed reason=%r (trace_id=%s)",
            action, reason, trace_id,
        )
        if reason == "release_failed":
            # wh-mouse-grid.1.19: preserve WHICH button may still be held.
            # The recovery toast prescribes a grid cell click, and a plain
            # (left) click sends LEFTDOWN+LEFTUP -- it cannot release a stuck
            # RIGHT button. Logic requested the action, so the request params
            # carry the button identity; no wire schema change needed.
            if request.get("button") == "right":
                _notice("grid_right_button_release_failed")
            else:
                _notice("grid_button_release_failed")
            return
        if reason == "disabled_by_config":
            _notice("disabled_by_config")
            return
        _notice(failure_reason)

    async def grid_pointer_channel_quiescent(self, *, trace_id: str) -> bool:
        """Wait until no grid pointer action can still act on the desktop.

        wh-mouse-grid.1.29: called before a session-visibility change
        (grid open / next_screen, numbered-overlay show, the ambiguous
        auto-open) so a stale click, move, or drag cannot execute at the
        prior cell coordinates after the user sees the new session --
        worst for a multi-second drag pressing and moving under a fresh
        grid. Three steps, each necessary:

          1. Await every task ``track_pending_pointer_task`` recorded
             (grid click / move / drag, and numbered-badge clicks --
             wh-mouse-grid.1.30). This covers a dispatched task that has
             not yet reached the lock.
          2. Take ``_grid_mouse_action_lock``. An in-flight
             ``_send_mouse_action`` holds it from admission through its
             outcome (including the timeout-recovery probe), so holding
             it proves no send is mid-flight.
          3. Under the lock, when the channel is suspect after an
             earlier timeout -- from a grid pointer action, a by-name
             click_element, or a badge click_snapshot_item
             (wh-mouse-grid.1.30) -- confirm it with the probe: Input's
             command loop is single-threaded and in-order, so any probe
             reply proves everything queued before it either executed or
             was overwritten and can never act later.

        Returns False when the probe cannot confirm the channel -- a
        queued payload may still execute, so the caller must withhold
        the new session (the same policy the next pointer action
        follows). Fast path: nothing pending, lock free, flag clear --
        no probe, no wait. Never raises.
        """
        pending = getattr(self, "_pending_grid_mouse_actions", None)
        while pending:
            await asyncio.wait(set(pending))
        lock = getattr(self, "_grid_mouse_action_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._grid_mouse_action_lock = lock
        async with lock:
            if not self._grid_mouse_channel_suspect:
                return True
            timeout_ms = self._host.click_config.response_timeout_ms
            timeout_s = max(timeout_ms / 1000.0, 0.1)
            return await self._confirm_grid_mouse_channel(
                timeout_s=timeout_s, trace_id=trace_id,
            ) is not None

    async def _confirm_grid_mouse_channel(
        self, *, timeout_s: float, trace_id: str,
    ) -> Optional[dict]:
        """Probe the Input command channel after a grid send fault.

        Called under ``_grid_mouse_action_lock`` when the channel is suspect
        (wh-mouse-grid.1.18). Sends a ``channel_probe`` through the same
        request path the pointer actions use and awaits its reply. Input
        executes commands one at a time in arrival order, so ANY reply --
        including an error reply -- proves the previously timed-out payload
        has left the pipeline: it either already executed or was overwritten
        in shared memory before Input copied it, and in both cases it can no
        longer act on the desktop later.

        Returns the probe's reply dict on success (its ``last_mouse_action``
        key carries Input's record of the most recent pointer-action result,
        which the timeout-recovery path correlates by trace_id --
        wh-mouse-grid.1.24), after clearing the suspect flag. Returns
        ``None`` on a probe timeout or send failure: the flag stays set and
        the caller must refuse. Never raises.
        """
        try:
            reply = await self._host.app.send_request(
                "channel_probe",
                params={"trace_id": trace_id},
                timeout_s=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 -- refusal, not a crash
            logger.warning(
                "grid: channel_probe got no reply (%s); channel stays "
                "suspect (trace_id=%s)",
                type(exc).__name__, trace_id,
            )
            return None
        logger.info(
            "grid: channel_probe confirmed the Input channel drained "
            "(trace_id=%s)", trace_id,
        )
        self._grid_mouse_channel_suspect = False
        return reply if isinstance(reply, dict) else {}
