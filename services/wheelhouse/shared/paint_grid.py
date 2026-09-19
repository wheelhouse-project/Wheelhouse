"""PaintGridEvent Logic -> GUI event schema (wh-grid-state-machine).

Defines the Logic-to-GUI event that draws the mouse grid: nine numbered
cells subdividing one rectangle on one monitor. The authoritative spec is
``docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md`` ("GUI
process -- painting only": Logic sends "paint this rectangle as a grid",
the GUI draws and decides nothing).

Lifecycle: whenever the grid opens, a spoken number refines it, "mark"
resets it, or "grid next screen" moves it, the Logic-side
``grid_overlay_state.GridOverlayStateMachine`` returns a ``PAINT_GRID``
effect and the integration sends this event as the ``paint_grid`` action.
Each event is a complete description of what should be on screen, so the
GUI holds no grid state of its own and a dropped event is repaired by the
next one.

COORDINATES -- every field is in virtual-desktop PHYSICAL pixels, the
coordinate system ``EnumDisplayMonitors`` / ``GetMonitorInfo`` report (see
``shared/monitor_geometry.py``) and the one UIA bounding rectangles use.
``monitor_left`` / ``monitor_top`` / ``left`` / ``top`` MAY be negative --
a monitor left of or above the primary has a negative origin. The four
size fields must be positive.

TWO RECTANGLES, both needed. ``monitor_*`` says which monitor the grid
belongs to, so the GUI can pick the right per-monitor overlay window and
convert to that screen's logical coordinates (the numbered overlay's
``shared/overlay_dpi_resolver.py`` does the same job for badges).
``left`` / ``top`` / ``width`` / ``height`` is the CURRENT rectangle --
the whole monitor when the grid opens, a cell of a cell after a few
refinements -- and it is what gets divided into nine. The GUI does not
compute the subdivision from the monitor: refinement state lives in Logic.

The fields are eight flat ints rather than a nested rectangle type
because the alternative would be for this shared schema module to import
the Logic-process ``grid_overlay_state.GridRect``, inverting the
dependency (Logic imports schemas, not the reverse). The producer unpacks
``GridRect.as_tuple()`` into these fields.

Transport: Logic puts a dict produced by ``PaintGridEvent.to_dict()`` onto
the GUI command queue. The GUI validates inbound via
``safe_parse(PaintGridEvent.from_dict, command, ...)`` (wh-uf54) so a
malformed payload is logged and dropped rather than crashing the GUI
command listener. ``from_dict`` never lets a raw ``KeyError`` /
``TypeError`` / ``AttributeError`` escape -- every structural problem
surfaces as ``PaintGridEventSchemaError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.schema_guard import reraise_as_schema_error


ACTION_NAME = "paint_grid"


class PaintGridEventSchemaError(ValueError):
    """Raised by ``PaintGridEvent`` on a bad payload or a bad producer value.

    The GUI process should catch this via ``safe_parse`` and degrade
    gracefully (log + drop), per wh-uf54. ``from_dict`` never lets a raw
    ``KeyError`` / ``TypeError`` / ``AttributeError`` escape. ``to_dict``
    raises it too, so an unpaintable rectangle fails at the producer
    rather than being silently dropped by the consumer -- the same
    reasoning as ``PaintOverlayEvent.to_dict``'s ``summary=None`` guard.
    """


# The four coordinate fields, which may be negative, and the four size
# fields, which must be positive. Kept as module constants so ``to_dict``
# and ``from_dict`` validate exactly the same set.
_COORDINATE_FIELDS = ("monitor_left", "monitor_top", "left", "top")
_SIZE_FIELDS = ("monitor_width", "monitor_height", "width", "height")


@dataclass(frozen=True)
class PaintGridEvent:
    """Structured Logic -> GUI paint_grid event."""

    monitor_left: int
    monitor_top: int
    monitor_width: int
    monitor_height: int
    left: int
    top: int
    width: int
    height: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        The ``"action"`` key carries ``ACTION_NAME`` so the GUI dispatch
        can route it. Raises ``PaintGridEventSchemaError`` when a size is
        not a positive int: a zero-or-negative rectangle has no pixels to
        draw nine cells in, and emitting one would leave the GUI to drop
        the event at ``from_dict`` with the grid still painted from the
        previous event.
        """

        # Producer and consumer validate the SAME field set
        # (wh-mouse-grid.1.23): a coordinate carrying a non-int used to
        # serialize fine here, the GUI's safe_parse then dropped it at
        # from_dict, and the paint manager kept showing the PREVIOUS grid
        # cell while Logic's state had advanced -- the next "click" lands in
        # a cell the user never saw. Coordinates may be negative (a monitor
        # left of or above the primary), so they get the int-not-bool check
        # without the positivity check.
        for field in _COORDINATE_FIELDS + _SIZE_FIELDS:
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise PaintGridEventSchemaError(
                    f"field {field!r} must be an int, "
                    f"got {type(value).__name__}"
                )
        for field in _SIZE_FIELDS:
            value = getattr(self, field)
            if value <= 0:
                raise PaintGridEventSchemaError(
                    f"field {field!r} must be positive, got {value}"
                )
        return {
            "action": ACTION_NAME,
            "monitor_left": self.monitor_left,
            "monitor_top": self.monitor_top,
            "monitor_width": self.monitor_width,
            "monitor_height": self.monitor_height,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    @reraise_as_schema_error(PaintGridEventSchemaError)
    def from_dict(cls, payload: Any) -> "PaintGridEvent":
        """Parse and validate a wire-format dict.

        Raises ``PaintGridEventSchemaError`` on any structural problem:
        not a mapping, missing or wrong ``"action"``, a missing or
        non-int coordinate or size (``bool`` excluded -- it is a subclass
        of ``int``), or a size that is not positive.
        """

        if not isinstance(payload, Mapping):
            raise PaintGridEventSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "action" not in payload:
            raise PaintGridEventSchemaError(
                "payload missing required key 'action'"
            )
        if payload["action"] != ACTION_NAME:
            raise PaintGridEventSchemaError(
                f"payload action {payload['action']!r} does not match "
                f"{ACTION_NAME!r}"
            )

        values: dict[str, int] = {}
        for field in _COORDINATE_FIELDS:
            values[field] = _require_int(payload, field)
        for field in _SIZE_FIELDS:
            size = _require_int(payload, field)
            if size <= 0:
                raise PaintGridEventSchemaError(
                    f"field {field!r} must be positive, got {size}"
                )
            values[field] = size

        return cls(**values)


def _require_int(payload: Mapping[str, Any], field: str) -> int:
    """Return ``payload[field]`` as an int, excluding ``bool``.

    Raises ``PaintGridEventSchemaError`` when the field is absent, not an
    int, or a ``bool`` (a subclass of ``int``).
    """

    if field not in payload:
        raise PaintGridEventSchemaError(
            f"payload missing required field {field!r}"
        )
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaintGridEventSchemaError(
            f"field {field!r} must be an int, got {type(value).__name__}"
        )
    return value
