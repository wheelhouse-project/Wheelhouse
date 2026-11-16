"""The OverlayPaintWindowManager members that the P4 and P5 classes use.

OverlayPaintTeardown and OverlayGridPainter keep a back-reference to the
OverlayPaintWindowManager that built them and read these members through it.
The manager provides each member as a public property (or method) over a
private field. The fields stay on the manager because paint, the working
badge and the badge drawing (areas P1, P2 and P3) also use them.
OverlayGridPainter also calls destroy_all on the manager's
OverlayPaintTeardown (OverlayTeardownPort). This module lists them, so the
back-references are explicit and the type checker sees each use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from overlay_paint_window import GenerationGate, _OverlayWindow


class OverlayTeardownHost(Protocol):
    """The members of OverlayPaintWindowManager that OverlayPaintTeardown
    reads and writes. Plain attributes, because the teardown assigns them
    (the manager provides a property with a setter for each)."""

    gate: GenerationGate
    windows: dict[int, _OverlayWindow]
    pending_destroy: list[_OverlayWindow]


class OverlayGridHost(Protocol):
    """The members of OverlayPaintWindowManager that OverlayGridPainter
    reads and writes.

    The Any values: the manager keeps ctypes.windll.user32 and
    ctypes.windll.kernel32 in the fields _user32 and _kernel32, which have no
    declared type, and OverlayPaintWindowManager._ensure_class_registered
    declares Any for the module handle that GetModuleHandleW gave.
    """

    windows: dict[int, _OverlayWindow]
    pending_destroy: list[_OverlayWindow]

    @property
    def class_name(self) -> str: ...

    @property
    def user32(self) -> Any: ...

    @property
    def kernel32(self) -> Any: ...

    @property
    def badge_shadow(self) -> bool: ...

    def ensure_class_registered(self) -> Any: ...


class OverlayTeardownPort(Protocol):
    """The member of OverlayPaintTeardown that OverlayGridPainter calls."""

    def destroy_all(self) -> bool: ...
