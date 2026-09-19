"""PaintGridPinEvent Logic -> GUI event schema (wh-grid-state-machine).

Defines the Logic-to-GUI event that draws the mouse grid's drag anchor --
the pin the user places by saying "mark", which stays on screen while
they navigate the grid to the drag's destination. The authoritative spec
is ``docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md``
(the actions table's "mark" row, and "GUI process -- painting only":
Logic sends "paint a pin at this point").

Lifecycle: "mark" makes the Logic-side
``grid_overlay_state.GridOverlayStateMachine`` return a ``PAINT_PIN``
effect carrying the current cell's center, and the integration sends this
event as the ``paint_grid_pin`` action. It is re-sent whenever the grid
is repainted from scratch (a re-said "show grid", "grid next screen") so
the GUI never has to remember the pin across a repaint. The pin is
removed by ``ClearGridEvent``, never by an event of its own: the pin dies
with the grid session, and one teardown message for both keeps that
guarantee impossible to break by dropping a message.

COORDINATES -- ``x`` / ``y`` are in virtual-desktop PHYSICAL pixels, the
coordinate system ``EnumDisplayMonitors`` / ``GetMonitorInfo`` report (see
``shared/monitor_geometry.py``). Both MAY be negative: the pin can sit on
a monitor left of or above the primary, and a drag whose two endpoints
are on different monitors is legitimate. There is deliberately no monitor
field -- the point alone identifies the monitor, and requiring the
producer to also name one would create a second source of truth that
could disagree.

Transport: Logic puts a dict produced by ``PaintGridPinEvent.to_dict()``
onto the GUI command queue. The GUI validates inbound via
``safe_parse(PaintGridPinEvent.from_dict, command, ...)`` (wh-uf54) so a
malformed payload is logged and dropped rather than crashing the GUI
command listener. ``from_dict`` never lets a raw ``KeyError`` /
``TypeError`` / ``AttributeError`` escape -- every structural problem
surfaces as ``PaintGridPinEventSchemaError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.schema_guard import reraise_as_schema_error


ACTION_NAME = "paint_grid_pin"


class PaintGridPinEventSchemaError(ValueError):
    """Raised by ``PaintGridPinEvent.from_dict`` on a bad payload.

    The GUI process should catch this via ``safe_parse`` and degrade
    gracefully (log + drop), per wh-uf54. ``from_dict`` never lets a raw
    ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.
    """


@dataclass(frozen=True)
class PaintGridPinEvent:
    """Structured Logic -> GUI paint_grid_pin event."""

    x: int
    y: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        The ``"action"`` key carries ``ACTION_NAME`` so the GUI dispatch
        can route it. Raises ``PaintGridPinEventSchemaError`` when a
        coordinate is not an int (``bool`` excluded): producer and consumer
        validate the same fields (wh-mouse-grid.1.25, the sibling of
        ``PaintGridEvent.to_dict``'s check) -- a bad mark point must fail
        here, loudly, rather than serialize, be dropped by the GUI's
        ``safe_parse``, and leave the PREVIOUS pin displayed while Logic
        holds the new mark, so a later "drag" would start from a point the
        user never saw.
        """

        for field in ("x", "y"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise PaintGridPinEventSchemaError(
                    f"field {field!r} must be an int, "
                    f"got {type(value).__name__}"
                )
        return {"action": ACTION_NAME, "x": self.x, "y": self.y}

    @classmethod
    @reraise_as_schema_error(PaintGridPinEventSchemaError)
    def from_dict(cls, payload: Any) -> "PaintGridPinEvent":
        """Parse and validate a wire-format dict.

        Raises ``PaintGridPinEventSchemaError`` on any structural
        problem: not a mapping, missing or wrong ``"action"``, or a
        missing / non-int ``"x"`` / ``"y"`` (``bool`` excluded -- it is a
        subclass of ``int``). No range check: any point on the virtual
        desktop is valid, including negative coordinates.
        """

        if not isinstance(payload, Mapping):
            raise PaintGridPinEventSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "action" not in payload:
            raise PaintGridPinEventSchemaError(
                "payload missing required key 'action'"
            )
        if payload["action"] != ACTION_NAME:
            raise PaintGridPinEventSchemaError(
                f"payload action {payload['action']!r} does not match "
                f"{ACTION_NAME!r}"
            )

        return cls(x=_require_int(payload, "x"), y=_require_int(payload, "y"))


def _require_int(payload: Mapping[str, Any], field: str) -> int:
    """Return ``payload[field]`` as an int, excluding ``bool``.

    Raises ``PaintGridPinEventSchemaError`` when the field is absent, not
    an int, or a ``bool`` (a subclass of ``int``).
    """

    if field not in payload:
        raise PaintGridPinEventSchemaError(
            f"payload missing required field {field!r}"
        )
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaintGridPinEventSchemaError(
            f"field {field!r} must be an int, got {type(value).__name__}"
        )
    return value
