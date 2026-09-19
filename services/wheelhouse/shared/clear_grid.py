"""ClearGridEvent Logic -> GUI event schema (wh-grid-state-machine).

Defines the Logic-to-GUI event that tears the mouse grid down. The
authoritative spec is
``docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md`` ("GUI
process -- painting only": Logic sends "clear"; the GUI draws and decides
nothing).

Lifecycle: every path that closes the grid -- "hide grid", an action
performed at the current cell center, a drag, the numbered overlay
opening, or the Logic-side recovery reset -- makes
``grid_overlay_state.GridOverlayStateMachine`` return a ``CLEAR_GRID``
effect, and the integration sends this event as the ``clear_grid``
action. ONE event removes both the grid and any pin: the pin dies with
the grid session by contract, and pairing that with a single teardown
message makes it impossible for a dropped message to leave a pin on
screen with no grid behind it.

NO FIELDS, deliberately. The numbered overlay's ``ClearOverlayEvent``
carries an ``(overlay_session_id, paint_generation)`` pair so the GUI can
ignore a stale clear that arrives after a newer paint. The grid needs no
such discipline: it has no in-flight build phase, so Logic never has more
than one grid outstanding, and every paint event is a complete
description of what should be on screen. A clear can therefore only ever
mean "nothing" -- and the failure the generation pair guards against
(a stale clear wiping a live overlay) cannot arise. The safe direction
for a fieldless clear is also the right one: if one were somehow
duplicated, clearing an already-clear screen is a no-op.

Transport: Logic puts a dict produced by ``ClearGridEvent.to_dict()``
onto the GUI command queue. The GUI validates inbound via
``safe_parse(ClearGridEvent.from_dict, command, ...)`` (wh-uf54) so a
malformed payload is logged and dropped rather than crashing the GUI
command listener. ``from_dict`` never lets a raw ``KeyError`` /
``TypeError`` / ``AttributeError`` escape -- every structural problem
surfaces as ``ClearGridEventSchemaError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.schema_guard import reraise_as_schema_error


ACTION_NAME = "clear_grid"


class ClearGridEventSchemaError(ValueError):
    """Raised by ``ClearGridEvent.from_dict`` on a bad payload.

    The GUI process should catch this via ``safe_parse`` and degrade
    gracefully (log + drop), per wh-uf54. ``from_dict`` never lets a raw
    ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.
    """


@dataclass(frozen=True)
class ClearGridEvent:
    """Structured Logic -> GUI clear_grid event. Carries no payload."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        The ``"action"`` key carries ``ACTION_NAME`` so the GUI dispatch
        can route it; there is nothing else to send.
        """

        return {"action": ACTION_NAME}

    @classmethod
    @reraise_as_schema_error(ClearGridEventSchemaError)
    def from_dict(cls, payload: Any) -> "ClearGridEvent":
        """Parse and validate a wire-format dict.

        Raises ``ClearGridEventSchemaError`` when the payload is not a
        mapping or its ``"action"`` is missing or wrong. Unknown extra
        keys are IGNORED rather than rejected: a clear that gets dropped
        leaves the grid painted on screen with nothing driving it, which
        is the worst outcome this schema has, so a future producer that
        adds a field must not be able to strand an older GUI.
        """

        if not isinstance(payload, Mapping):
            raise ClearGridEventSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "action" not in payload:
            raise ClearGridEventSchemaError(
                "payload missing required key 'action'"
            )
        if payload["action"] != ACTION_NAME:
            raise ClearGridEventSchemaError(
                f"payload action {payload['action']!r} does not match "
                f"{ACTION_NAME!r}"
            )

        return cls()
