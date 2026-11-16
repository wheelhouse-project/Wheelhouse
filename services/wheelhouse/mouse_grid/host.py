"""The LogicController members that MouseGridController uses.

MouseGridController keeps a back-reference to the LogicController that built
it and reads these members through it. They stay on LogicController because
code outside the mouse grid also uses them (the numbered overlay, the by-name
click and the host members of numbered_overlay/host.py). This Protocol lists
them, so the back-reference is explicit and the type checker sees each use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Protocol, TypeVar

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable, Coroutine

    from services.wheelhouse.grid_overlay_state import GridOverlayStateMachine
    from services.wheelhouse.state_manager import StateManager
    from services.wheelhouse.ui.click_config import ClickConfig

_T = TypeVar("_T")


class MouseGridRequestSender(Protocol):
    """The one member of WheelHouseApp that MouseGridController calls.

    The Any values of the dicts: WheelHouseApp.send_request (app.py) declares
    Dict[str, Any] for the params, the late reply and the reply.
    """

    async def send_request(
        self,
        action: str,
        params: Optional[dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
        on_late_response: Optional[Callable[[dict[str, Any]], None]] = None,
        quiet_timeout: bool = False,
        delivery_ttl_s: Optional[float] = None,
    ) -> dict[str, Any]: ...


class MouseGridOverlay(Protocol):
    """The members of NumberedOverlayController that MouseGridController calls."""

    def forward_click_notice(
        self,
        *,
        outcome: str,
        reason: Optional[str],
        matched_name: Optional[str],
        matched_names: tuple[str, ...],
        spoken_name: str,
        snapshot_id: Optional[str],
        trace_id: str,
        late_correction: bool = False,
    ) -> None: ...

    async def handle_overlay_command(
        self, command: str, trace_id: str,
    ) -> Optional[asyncio.Task]: ...


class MouseGridHost(Protocol):
    """The members of LogicController that MouseGridController reads or calls."""

    app: MouseGridRequestSender
    click_config: ClickConfig
    overlay: MouseGridOverlay

    # Read-only properties, not plain attributes: the grid code only reads
    # them, and a property lets the non-None attribute of LogicController
    # satisfy the Optional type that test hosts need.
    @property
    def state_manager(self) -> Optional[StateManager]: ...

    @property
    def grid_overlay_state(self) -> Optional[GridOverlayStateMachine]: ...

    def take_click_disabled_notice(self) -> bool: ...

    # The Any arguments of Coroutine: LogicController.create_task_with_error_handling
    # (main.py) declares no type for coro.
    def create_task_with_error_handling(
        self, coro: Coroutine[Any, Any, _T], task_name: str,
    ) -> asyncio.Task[_T]: ...
