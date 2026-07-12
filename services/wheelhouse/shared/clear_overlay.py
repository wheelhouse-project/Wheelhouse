"""ClearOverlayEvent Logic -> GUI event schema (wh-9gkh5k).

Defines the Logic-to-GUI event that tears down the numbered overlay during
Phase 1.5 of the voice-element-clicking feature (epic wh-l4h.1). The
authoritative field spec lives in the v4 design doc:
``docs/plans/2026-05-28-voice-element-clicking-phase-1-5-design-v4.md``
under "New schemas (Phase 1.5 only)".

Lifecycle: when Logic needs the GUI to remove the painted numbered overlay
(hide-numbers, click resolution, focus change, session teardown), it sends
this event as the ``clear_overlay`` action. The ``overlay_session_id`` ties
the clear to a session and the ``paint_generation`` lets the GUI ignore a
stale clear that arrives after a newer paint (r1c.2).

Transport: Logic puts a dict produced by ``ClearOverlayEvent.to_dict()``
onto the GUI command queue. The GUI validates inbound via
``safe_parse(ClearOverlayEvent.from_dict, command, ...)`` (wh-uf54) so a
malformed payload is logged and dropped rather than crashing the GUI
command listener. ``from_dict`` never lets a raw ``KeyError`` /
``TypeError`` / ``AttributeError`` escape -- every structural problem
surfaces as ``ClearOverlayEventSchemaError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.schema_guard import reraise_as_schema_error


ACTION_NAME = "clear_overlay"


class ClearOverlayEventSchemaError(ValueError):
    """Raised by ``ClearOverlayEvent.from_dict`` on a bad payload.

    The GUI process should catch this via ``safe_parse`` and degrade
    gracefully (log + drop), per wh-uf54. ``from_dict`` never lets a raw
    ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.
    """


@dataclass(frozen=True)
class ClearOverlayEvent:
    """Structured Logic -> GUI clear_overlay event."""

    overlay_session_id: int
    paint_generation: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        The ``"action"`` key carries ``ACTION_NAME`` so the GUI dispatch can
        route it.
        """

        return {
            "action": ACTION_NAME,
            "overlay_session_id": self.overlay_session_id,
            "paint_generation": self.paint_generation,
        }

    @classmethod
    @reraise_as_schema_error(ClearOverlayEventSchemaError)
    def from_dict(cls, payload: Any) -> "ClearOverlayEvent":
        """Parse and validate a wire-format dict.

        Raises ``ClearOverlayEventSchemaError`` on any structural problem:
        not a mapping, missing or wrong ``"action"``, or missing / non-int
        ``"overlay_session_id"`` / ``"paint_generation"`` (``bool`` excluded
        -- it is a subclass of ``int``).
        """

        if not isinstance(payload, Mapping):
            raise ClearOverlayEventSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "action" not in payload:
            raise ClearOverlayEventSchemaError(
                "payload missing required key 'action'"
            )
        if payload["action"] != ACTION_NAME:
            raise ClearOverlayEventSchemaError(
                f"payload action {payload['action']!r} does not match "
                f"{ACTION_NAME!r}"
            )

        overlay_session_id = _require_int(payload, "overlay_session_id")
        paint_generation = _require_int(payload, "paint_generation")

        return cls(
            overlay_session_id=overlay_session_id,
            paint_generation=paint_generation,
        )


def _require_int(payload: Mapping[str, Any], field: str) -> int:
    """Return ``payload[field]`` as an int, excluding ``bool``.

    Raises ``ClearOverlayEventSchemaError`` when the field is absent, not an
    int, or a ``bool`` (a subclass of ``int``).
    """

    if field not in payload:
        raise ClearOverlayEventSchemaError(
            f"payload missing required field {field!r}"
        )
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClearOverlayEventSchemaError(
            f"field {field!r} must be an int, got {type(value).__name__}"
        )
    return value
