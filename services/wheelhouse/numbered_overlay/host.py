"""The LogicController members that the numbered overlay uses.

NumberedOverlayController keeps a back-reference to LogicController and reads
these members through it when it needs them. LogicController builds the
overlay inside its own __init__, and it assigns some members only after that
point (grid_overlay_state), so the overlay cannot copy them when it is built.
This Protocol lists them, so the back-reference is explicit and the type
checker sees each use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Protocol, TypeVar

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Coroutine
    from multiprocessing.synchronize import Event as MpEvent

    from services.wheelhouse.app import WheelHouseApp
    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.grid_overlay_state import GridOverlayStateMachine
    from services.wheelhouse.state_manager import StateManager
    from services.wheelhouse.ui.click_config import ClickConfig
    from services.wheelhouse.ui.element_types import ClickGesture

_T = TypeVar("_T")


class OverlayHost(Protocol):
    """The members of LogicController that NumberedOverlayController reads."""

    app: WheelHouseApp
    click_config: ClickConfig
    config_service: ConfigService
    loop: asyncio.AbstractEventLoop
    shutdown_event: MpEvent
    state_manager: StateManager

    # A read-only property, not a plain attribute: the overlay only reads it,
    # and a property lets LogicController's non-None attribute satisfy the
    # Optional type that test hosts need. A plain Optional attribute is
    # invariant, so Pyright then rejects LogicController.
    @property
    def grid_overlay_state(self) -> Optional[GridOverlayStateMachine]: ...

    def create_task_with_error_handling(
        self, coro: Coroutine[Any, Any, _T], task_name: str,
    ) -> asyncio.Task[_T]: ...

    async def grid_pointer_channel_quiescent(self, *, trace_id: str) -> bool: ...

    def grid_session_in_transition(self) -> bool: ...

    async def handle_grid_command(
        self, command: str, trace_id: str, *,
        number: int = 0,
        gesture: Optional[ClickGesture] = None,
        spoken: str = "",
    ) -> bool: ...

    def mark_grid_mouse_channel_suspect(self) -> None: ...

    def mark_screen_read_finished(self) -> None: ...

    def mark_screen_read_started(self) -> None: ...

    def put_grid_gui_event(self, payload: dict, trace_id: str) -> None: ...

    def speech_sentence_closed_event(self) -> Optional[asyncio.Event]: ...

    def take_click_disabled_notice(self) -> bool: ...

    def track_pending_pointer_task(self, task: asyncio.Task[None]) -> None: ...

    def wheelhouse_process_ids(self) -> list[int]: ...
