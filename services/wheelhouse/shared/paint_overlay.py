"""PaintOverlayEvent Logic -> GUI event schema (wh-9gkh5k).

Defines the Logic-to-GUI event that drives the numbered-overlay paint
during Phase 1.5 of the voice-element-clicking feature (epic wh-l4h.1).
The authoritative field spec lives in the v4 design doc:
``docs/plans/2026-05-28-voice-element-clicking-phase-1-5-design-v4.md``
under "New schemas (Phase 1.5 only)".

Lifecycle: when Logic has a ``WalkSnapshotSummary`` ready to display, it
sends this event to the GUI as the ``paint_overlay`` action; the GUI paints
the click-through numbered overlay over the matched controls. The
``overlay_session_id`` ties the paint to a session and the
``paint_generation`` lets the GUI drop a superseded paint (r1c.2).

WIRE SHAPE -- the ``WalkSnapshotSummary`` is FLATTENED into the top-level
wire dict, NOT nested under a ``snapshot_summary`` key (this differs from
``ShowNumberedOverlayResponse``, which nests it). ``summary_to_dict``
returns ``snapshot_id`` / ``created_at_monotonic`` / ``items``, so those
three keys appear at the TOP level of the ``paint_overlay`` dict alongside
``action`` / ``overlay_session_id`` / ``paint_generation``.

Transport: Logic puts a dict produced by ``PaintOverlayEvent.to_dict()``
onto the GUI command queue. The GUI validates inbound via
``safe_parse(PaintOverlayEvent.from_dict, command, ...)`` (wh-uf54) so a
malformed payload is logged and dropped rather than crashing the GUI
command listener. ``from_dict`` never lets a raw ``KeyError`` /
``TypeError`` / ``AttributeError`` escape -- every structural problem
surfaces as ``PaintOverlayEventSchemaError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.schema_guard import reraise_as_schema_error

from ui.element_types import WalkSnapshotSummary
from services.wheelhouse.shared.walk_snapshot_serde import (
    summary_from_dict,
    summary_to_dict,
)


ACTION_NAME = "paint_overlay"


class PaintOverlayEventSchemaError(ValueError):
    """Raised by ``PaintOverlayEvent.from_dict`` on a bad payload.

    The GUI process should catch this via ``safe_parse`` and degrade
    gracefully (log + drop), per wh-uf54. ``from_dict`` never lets a raw
    ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.
    """


@dataclass(frozen=True)
class PaintOverlayEvent:
    """Structured Logic -> GUI paint_overlay event."""

    overlay_session_id: int
    paint_generation: int
    summary: WalkSnapshotSummary

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        The ``WalkSnapshotSummary`` is FLATTENED into the top-level dict via
        ``**summary_to_dict(self.summary)`` so ``snapshot_id`` /
        ``created_at_monotonic`` / ``items`` ride at the top level. The
        ``"action"`` key carries ``ACTION_NAME`` so the GUI dispatch can
        route it.
        """

        summary_fields = summary_to_dict(self.summary)
        if summary_fields is None:
            # summary is a non-optional WalkSnapshotSummary; a None here means a
            # producer constructed the event with summary=None in violation of
            # the type. Fail loudly rather than emit a paint_overlay dict with
            # the flattened summary fields missing -- such a payload would be
            # dropped by the GUI at from_dict, leaving Logic waiting for an
            # overlay ack that never arrives (wh-n29v.3.1).
            raise PaintOverlayEventSchemaError(
                "summary must be a WalkSnapshotSummary, not None"
            )
        return {
            "action": ACTION_NAME,
            "overlay_session_id": self.overlay_session_id,
            "paint_generation": self.paint_generation,
            **summary_fields,
        }

    @classmethod
    @reraise_as_schema_error(PaintOverlayEventSchemaError)
    def from_dict(cls, payload: Any) -> "PaintOverlayEvent":
        """Parse and validate a wire-format dict.

        Raises ``PaintOverlayEventSchemaError`` on any structural problem:
        not a mapping, missing or wrong ``"action"``, missing or non-int
        ``"overlay_session_id"`` / ``"paint_generation"`` (``bool`` excluded
        -- it is a subclass of ``int``), or a malformed flattened summary
        (the ``snapshot_id`` / ``created_at_monotonic`` / ``items`` keys are
        reconstructed by ``summary_from_dict``).
        """

        if not isinstance(payload, Mapping):
            raise PaintOverlayEventSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "action" not in payload:
            raise PaintOverlayEventSchemaError(
                "payload missing required key 'action'"
            )
        if payload["action"] != ACTION_NAME:
            raise PaintOverlayEventSchemaError(
                f"payload action {payload['action']!r} does not match "
                f"{ACTION_NAME!r}"
            )

        overlay_session_id = _require_int(
            payload, "overlay_session_id"
        )
        paint_generation = _require_int(payload, "paint_generation")

        # The summary fields are flattened at the top level of the payload,
        # so summary_from_dict reads its own three keys (snapshot_id,
        # created_at_monotonic, items) directly off the whole payload and
        # raises PaintOverlayEventSchemaError on any nested problem. Since
        # payload is a Mapping (never None), this returns a real
        # WalkSnapshotSummary, never None.
        summary = summary_from_dict(payload, PaintOverlayEventSchemaError)
        if summary is None:  # pragma: no cover - defensive; payload is a Mapping
            raise PaintOverlayEventSchemaError(
                "payload could not be reconstructed into a WalkSnapshotSummary"
            )

        return cls(
            overlay_session_id=overlay_session_id,
            paint_generation=paint_generation,
            summary=summary,
        )


def _require_int(payload: Mapping[str, Any], field: str) -> int:
    """Return ``payload[field]`` as an int, excluding ``bool``.

    Raises ``PaintOverlayEventSchemaError`` when the field is absent, not an
    int, or a ``bool`` (a subclass of ``int``).
    """

    if field not in payload:
        raise PaintOverlayEventSchemaError(
            f"payload missing required field {field!r}"
        )
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaintOverlayEventSchemaError(
            f"field {field!r} must be an int, got {type(value).__name__}"
        )
    return value
