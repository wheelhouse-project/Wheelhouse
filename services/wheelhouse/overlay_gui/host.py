"""The GuiManager members that OverlayGuiMessages uses.

OverlayGuiMessages keeps a back-reference to the GuiManager that built it and
reads these members through it. They stay on GuiManager because GuiManager
builds them in __init__ (the two timers, the two paint window managers) and
other GuiManager code also uses them (commands_to_logic_queue, the numbered
overlay manager). This module also names each paint window manager by the
public methods that OverlayGuiMessages calls, so the back-reference is
explicit and the type checker sees each use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Protocol

if TYPE_CHECKING:
    from shared.paint_grid import PaintGridEvent
    from shared.paint_grid_pin import PaintGridPinEvent


class NumberedOverlayPaintPort(Protocol):
    """The members of the numbered-overlay OverlayPaintWindowManager that
    OverlayGuiMessages calls.

    The Any value of ``summary``: OverlayPaintWindowManager.paint declares
    Any (it reads a WalkSnapshotSummary by duck type). The dicts are the
    overlay_state_changed wire dicts (shared/overlay_state_changed.py).
    """

    def paint(
        self,
        summary: Any,
        overlay_session_id: int,
        paint_generation: int,
    ) -> Optional[dict[str, Any]]: ...

    def clear(
        self,
        overlay_session_id: int,
        paint_generation: int,
    ) -> Optional[dict[str, Any]]: ...

    def expire_lease(
        self,
        overlay_session_id: int,
        paint_generation: int,
    ) -> Optional[dict[str, Any]]: ...

    def reset(self) -> None: ...

    def retry_teardown(self) -> Optional[dict[str, Any]]: ...

    @property
    def teardown_pending(self) -> bool: ...

    @property
    def has_deferred_teardown_ack(self) -> bool: ...


class GridPaintPort(Protocol):
    """The members of the mouse-grid OverlayPaintWindowManager that
    OverlayGuiMessages calls.

    OverlayGuiMessages parses each queue message into a PaintGridEvent or a
    PaintGridPinEvent before it calls the manager.
    """

    def paint_grid(self, event: PaintGridEvent) -> bool: ...

    def paint_grid_pin(self, event: PaintGridPinEvent) -> bool: ...

    def clear_grid(self) -> None: ...


class OverlayTimerPort(Protocol):
    """The members of a QTimer that OverlayGuiMessages calls."""

    def start(self, msec: int, /) -> None: ...

    def stop(self) -> None: ...


class LogicCommandQueuePort(Protocol):
    """The member of the GUI-to-Logic queue that OverlayGuiMessages calls.

    The queue carries dicts of many shapes. OverlayGuiMessages puts only the
    overlay_state_changed wire dict on it.
    """

    def put_nowait(self, obj: dict[str, Any], /) -> None: ...


class OverlayGuiHost(Protocol):
    """The members of GuiManager that OverlayGuiMessages reads.

    Read-only properties, not plain attributes: OverlayGuiMessages only
    reads them, and a property accepts the narrower GuiManager types.
    """

    @property
    def commands_to_logic_queue(self) -> LogicCommandQueuePort: ...

    @property
    def overlay_manager(self) -> Optional[NumberedOverlayPaintPort]: ...

    @property
    def grid_overlay(self) -> Optional[GridPaintPort]: ...

    @property
    def overlay_lease_timer(self) -> OverlayTimerPort: ...

    @property
    def overlay_teardown_retry_timer(self) -> OverlayTimerPort: ...
