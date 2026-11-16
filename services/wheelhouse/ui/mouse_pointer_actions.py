"""MousePointerActions: the mouse pointer actions of the Input process (area U7).

wh-full-refactor.6.2 (stage B of place 3) moved this code out of
UIActionHandler in ui/ui_action_handler.py without a change in behavior. The
stage A boundary tests (tests/test_mouse_grid_requirements_u7.py,
docs/design/mouse-grid-requirements.md, rows MP-n) pin that behavior.
UIActionHandler creates one instance as self.mouse_pointer and its four
actions call in through it; this class reads or calls the UIActionHandler members it
does not own through self._host (see mouse_pointer_host.py). It runs in the
Input process.
"""

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from .mouse_pointer_host import MousePointerHost

# The logger of ui_action_handler.py, by name: the moved code logs exactly the
# records it logged before the move.
logger = logging.getLogger(__name__.rpartition(".")[0] + ".ui_action_handler")


class MousePointerActions:
    """The click_point, click_at_pointer, move_pointer and perform_drag actions."""

    def __init__(
        self,
        host: "MousePointerHost",
        *,
        mouse_click_seam: Callable[[Any, Any, str, int], tuple[bool, Optional[str]]],
        mouse_move_seam: Callable[[Any, Any], tuple[bool, Optional[str]]],
        mouse_drag_seam: Callable[
            [Any, Any, Any, Any, Any], tuple[bool, Optional[str]]
        ],
        pointer_position_seam: Callable[[], Optional[tuple[int, int]]],
    ) -> None:
        """``host`` is the UIActionHandler that owns this object.

        The four seams are the SendInput calls of the actions
        (wh-input-mouse-primitives, wh-voice-access-parity.2.5).
        UIActionHandler passes the Win32-backed wrappers of
        ui/ui_action_handler.py; a test can replace an attribute with a
        fake, so no real input is sent. The point arguments are Any because
        the wrappers (_win32_click_point, _win32_move_pointer and
        _win32_drag_pointer in ui/ui_action_handler.py) declare no type for
        them: they pass the raw message values to the primitive, which
        validates them.
        """
        self._host = host
        self.mouse_click_seam = mouse_click_seam
        self.mouse_move_seam = mouse_move_seam
        self.mouse_drag_seam = mouse_drag_seam
        self.pointer_position_seam = pointer_position_seam
        #: The result of the most recent pointer action, or None before the
        #: first one. UIActionHandler.channel_probe sends it in its reply
        #: (wh-mouse-grid.1.24).
        self.last_mouse_action_result: Optional[dict[str, Any]] = None

    def _emit_mouse_action_response(
        self,
        *,
        action_name: str,
        request_id: Optional[str],
        trace_id: str,
        succeeded: bool,
        reason: Optional[str],
    ) -> None:
        """Put exactly one ``MouseActionResponse`` on the response queue.

        A dead response queue is the ONE case that yields zero responses:
        there is no second channel to deliver on, and the orphaned Logic
        Future is already covered by Logic's own timeout.
        """
        from services.wheelhouse.shared.mouse_action import MouseActionResponse

        payload = MouseActionResponse(
            status="ok" if succeeded else "error",
            outcome="ok" if succeeded else "execution_failed",
            reason=None if succeeded else (reason or "unexpected_error"),
            trace_id=trace_id if isinstance(trace_id, str) else "",
        ).to_dict()
        # Record the result BEFORE the enqueue attempt (wh-mouse-grid.1.24):
        # when the reply reaches Logic after its timeout, the app demuxer
        # discards it, so channel_probe's reply carries this record and Logic
        # correlates it by trace_id to recover a release_failed warning. A
        # failed enqueue is exactly the case where recovery matters, so the
        # record must not depend on the put succeeding. This command loop is
        # single-threaded, so the read in channel_probe cannot race this
        # write.
        self.last_mouse_action_result = {
            "action": action_name,
            "succeeded": bool(succeeded),
            "reason": payload["reason"],
            "trace_id": payload["trace_id"],
        }
        if request_id is not None:
            payload["request_id"] = request_id
        payload["action"] = action_name
        try:
            self._host.response_queue.put(payload)
        except Exception as exc:  # noqa: BLE001 -- log and drop
            logger.error(
                "%s: failed to enqueue response (trace_id=%s): %s",
                action_name, trace_id, exc,
            )

    def click_point(
        self,
        x,
        y,
        button: str,
        click_count: int,
        trace_id: str,
        request_id: Optional[str],
        **kwargs,
    ) -> None:
        """Click at a physical-pixel screen point (mouse-grid gesture).

        Args:
            x, y: physical screen pixels. Negative values are normal on a
                multi-monitor desktop whose secondary monitor sits left of or
                above the primary.
            button: ``"left"`` or ``"right"``.
            click_count: 1, 2 (a double click) or 3 (a triple click).
            trace_id: Logic-generated correlation id, echoed in the response.

        Emits one ``MouseActionResponse``. The argument validation lives in
        the SendInput primitive, which reports ``invalid_point`` /
        ``invalid_button`` / ``invalid_click_count`` rather than raising, so a
        malformed IPC message produces a normal ``execution_failed`` response.
        """
        action_name = "click_point"
        try:
            if not self._host.get_validated_click_config().grid_enabled_effective:
                logger.info(
                    "click_point: voice clicking disabled by config; "
                    "short-circuiting (trace_id=%s)", trace_id,
                )
                self._emit_mouse_action_response(
                    action_name=action_name, request_id=request_id,
                    trace_id=trace_id, succeeded=False,
                    reason="disabled_by_config",
                )
                return

            # wh-review-pattern-fixes.8: a grid click can move focus, the
            # caret, or the selection; invalidate before the dispatch (see
            # the hotkey_action block comment for the full rationale).
            self._host.buffer_manager.invalidate()
            succeeded, reason = self.mouse_click_seam(
                x, y, button, click_count,
            )
            logger.info(
                "click_point: point=(%r,%r) button=%s count=%r ok=%s "
                "reason=%s (trace_id=%s)",
                x, y, button, click_count, succeeded, reason, trace_id,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=bool(succeeded), reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise handler
            logger.error(
                "click_point: unexpected error (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=False,
                reason="unexpected_error",
            )

    def click_at_pointer(
        self,
        button: str,
        click_count: int,
        trace_id: str,
        request_id: Optional[str],
        **kwargs,
    ) -> None:
        """Click wherever the pointer is now (wh-voice-access-parity.2.5).

        The bare "click", "right click", "double click" and "triple click"
        words send this when no mouse grid is open. The pointer position is
        read HERE, in the Input process, through ``pointer_position_seam``
        (``GetCursorPos``), and the click goes through the same
        ``mouse_click_seam`` as ``click_point``, so the button and count
        validation and the stuck-button handling are the primitive's.

        Gate: the ``[click] enabled`` master switch only. Unlike the grid
        handlers this does not read ``grid_enabled_effective``: a bad grid
        key refuses the grid's geometry, and this action uses none.

        Emits one ``MouseActionResponse``. An unreadable pointer fails
        closed with ``pointer_unavailable``, sending no input.
        """
        action_name = "click_at_pointer"
        try:
            if not self._host.get_validated_click_config().enabled:
                logger.info(
                    "click_at_pointer: voice clicking disabled by config; "
                    "short-circuiting (trace_id=%s)", trace_id,
                )
                self._emit_mouse_action_response(
                    action_name=action_name, request_id=request_id,
                    trace_id=trace_id, succeeded=False,
                    reason="disabled_by_config",
                )
                return

            position = self.pointer_position_seam()
            if position is None:
                logger.warning(
                    "click_at_pointer: the pointer position could not be "
                    "read; sending no input (trace_id=%s)", trace_id,
                )
                self._emit_mouse_action_response(
                    action_name=action_name, request_id=request_id,
                    trace_id=trace_id, succeeded=False,
                    reason="pointer_unavailable",
                )
                return
            x, y = position

            # Same invalidation as click_point: a click can move focus, the
            # caret, or the selection (wh-review-pattern-fixes.8).
            self._host.buffer_manager.invalidate()
            succeeded, reason = self.mouse_click_seam(
                x, y, button, click_count,
            )
            logger.info(
                "click_at_pointer: point=(%r,%r) button=%s count=%r ok=%s "
                "reason=%s (trace_id=%s)",
                x, y, button, click_count, succeeded, reason, trace_id,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=bool(succeeded), reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise handler
            logger.error(
                "click_at_pointer: unexpected error (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=False,
                reason="unexpected_error",
            )

    def move_pointer(
        self,
        x,
        y,
        trace_id: str,
        request_id: Optional[str],
        **kwargs,
    ) -> None:
        """Park the pointer at a physical-pixel point, pressing nothing.

        The grid's "move here" gesture. Hover-revealed menus are common in
        exactly the applications with poor accessibility trees that motivate
        the grid, so parking the pointer is a first-class action rather than a
        side effect of clicking.

        Emits one ``MouseActionResponse``.
        """
        action_name = "move_pointer"
        try:
            if not self._host.get_validated_click_config().grid_enabled_effective:
                logger.info(
                    "move_pointer: voice clicking disabled by config; "
                    "short-circuiting (trace_id=%s)", trace_id,
                )
                self._emit_mouse_action_response(
                    action_name=action_name, request_id=request_id,
                    trace_id=trace_id, succeeded=False,
                    reason="disabled_by_config",
                )
                return

            succeeded, reason = self.mouse_move_seam(x, y)
            logger.info(
                "move_pointer: point=(%r,%r) ok=%s reason=%s (trace_id=%s)",
                x, y, succeeded, reason, trace_id,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=bool(succeeded), reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise handler
            logger.error(
                "move_pointer: unexpected error (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=False,
                reason="unexpected_error",
            )

    def perform_drag(
        self,
        start_x,
        start_y,
        end_x,
        end_y,
        duration_ms: int,
        trace_id: str,
        request_id: Optional[str],
        **kwargs,
    ) -> None:
        """Drag from one physical-pixel point to another as ONE operation.

        The whole gesture -- button down at the start, interpolated movement
        across ``duration_ms``, button up at the end -- runs inside the
        SendInput primitive, so no IPC boundary sits between the press and the
        release and no message loss can strand the button pressed. The
        primitive guarantees the release with a try/finally even when the
        movement fails partway.

        The movement is gradual because many applications ignore a drag whose
        pointer teleports: they never see the intermediate movement that makes
        them begin the drag operation.

        Emits one ``MouseActionResponse``. The ``release_failed`` reason is
        the one outcome meaning a mouse button may still be held; it reaches
        Logic verbatim so the notice can say so.
        """
        action_name = "perform_drag"
        try:
            if not self._host.get_validated_click_config().grid_enabled_effective:
                logger.info(
                    "perform_drag: voice clicking disabled by config; "
                    "short-circuiting (trace_id=%s)", trace_id,
                )
                self._emit_mouse_action_response(
                    action_name=action_name, request_id=request_id,
                    trace_id=trace_id, succeeded=False,
                    reason="disabled_by_config",
                )
                return

            # wh-review-pattern-fixes.8: a drag can move focus, the caret,
            # or the selection; invalidate before the dispatch (see the
            # hotkey_action block comment for the full rationale).
            self._host.buffer_manager.invalidate()
            succeeded, reason = self.mouse_drag_seam(
                start_x, start_y, end_x, end_y, duration_ms,
            )
            logger.info(
                "perform_drag: (%r,%r)->(%r,%r) duration_ms=%r ok=%s "
                "reason=%s (trace_id=%s)",
                start_x, start_y, end_x, end_y, duration_ms, succeeded,
                reason, trace_id,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=bool(succeeded), reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise handler
            logger.error(
                "perform_drag: unexpected error (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=False,
                reason="unexpected_error",
            )
