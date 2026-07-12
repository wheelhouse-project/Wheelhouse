"""snapshot_item_clicked GUI -> Logic event schema (wh-jfavj).

Defines the GUI-to-Logic event the GUI manager emits when the user clicks
a numbered overlay item during the Phase 1.5 voice-element-clicking flow
(epic wh-l4h.1). The authoritative lifecycle lives in the v5 design doc:
``docs/plans/2026-05-21-voice-element-clicking-design-v5.md`` under
"GUI-to-Logic round-trip (v5 added)".

Lifecycle (step 6 of the round-trip): the GUI paints click-through overlay
numbers, the user clicks number N, and the GUI emits this event carrying
the ``snapshot_id`` and the 1-based ``display_number`` N. Logic looks up
the retained ``WalkSnapshotSummary`` for that snapshot_id, resolves the
display number to the matching ``item_id``, and dispatches a
``click_snapshot_item(snapshot_id, item_id)`` request to the Input process.
The resolution happens in Logic so the GUI never tracks item_id -- the
overlay paints display numbers, the click reports a display number, and
Logic owns the display_number -> item_id mapping.

Transport: GuiManager.send_command puts a dict produced by
``SnapshotItemClickedEvent.to_dict()`` -- shape
``{"action": "snapshot_item_clicked", "snapshot_id": "...",
"display_number": N}`` -- onto the existing commands_to_logic_queue.
``LogicController._listen_for_gui_commands`` routes the action via its
handler_map; the handler validates the schema with :func:`safe_parse`
(wh-uf54) so a malformed payload is logged and dropped rather than
crashing the GUI command listener.

The numbered overlay is 1-based, so ``display_number`` must be ``>= 1``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.schema_guard import reraise_as_schema_error


ACTION_NAME = "snapshot_item_clicked"


class SnapshotItemClickedSchemaError(ValueError):
    """Raised by ``SnapshotItemClickedEvent.from_dict`` on a bad payload.

    The Logic handler should catch this via ``safe_parse`` and degrade
    gracefully (log + drop), per wh-uf54. ``from_dict`` never lets a raw
    ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.
    """


@dataclass(frozen=True)
class SnapshotItemClickedEvent:
    """Structured payload of a snapshot_item_clicked GUI -> Logic event."""

    snapshot_id: str
    display_number: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        The returned dict is the action payload that GuiManager.send_command
        puts onto commands_to_logic_queue; the ``"action"`` key carries
        ``ACTION_NAME`` so the existing dispatch in
        ``_listen_for_gui_commands`` can route it.
        """

        return {
            "action": ACTION_NAME,
            "snapshot_id": self.snapshot_id,
            "display_number": self.display_number,
        }

    @classmethod
    @reraise_as_schema_error(SnapshotItemClickedSchemaError)
    def from_dict(cls, payload: Any) -> "SnapshotItemClickedEvent":
        """Parse and validate a wire-format dict.

        Raises ``SnapshotItemClickedSchemaError`` on any structural problem:
        not a mapping, missing or wrong ``"action"``, missing or non-str
        ``"snapshot_id"``, missing or non-int ``"display_number"`` (``bool``
        excluded -- it is a subclass of ``int``), or a ``display_number``
        below 1 (the overlay is 1-based).
        """

        if not isinstance(payload, Mapping):
            raise SnapshotItemClickedSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "action" not in payload:
            raise SnapshotItemClickedSchemaError(
                "payload missing required key 'action'"
            )
        if payload["action"] != ACTION_NAME:
            raise SnapshotItemClickedSchemaError(
                f"payload action {payload['action']!r} does not match "
                f"{ACTION_NAME!r}"
            )

        if "snapshot_id" not in payload:
            raise SnapshotItemClickedSchemaError(
                "payload missing required field 'snapshot_id'"
            )
        snapshot_id = payload["snapshot_id"]
        if not isinstance(snapshot_id, str):
            raise SnapshotItemClickedSchemaError(
                "field 'snapshot_id' must be a str, "
                f"got {type(snapshot_id).__name__}"
            )

        if "display_number" not in payload:
            raise SnapshotItemClickedSchemaError(
                "payload missing required field 'display_number'"
            )
        display_number = payload["display_number"]
        # bool is a subclass of int; exclude it explicitly.
        if isinstance(display_number, bool) or not isinstance(
            display_number, int
        ):
            raise SnapshotItemClickedSchemaError(
                "field 'display_number' must be an int, "
                f"got {type(display_number).__name__}"
            )
        if display_number < 1:
            raise SnapshotItemClickedSchemaError(
                "field 'display_number' must be >= 1 (the overlay is "
                f"1-based), got {display_number}"
            )

        return cls(snapshot_id=snapshot_id, display_number=display_number)
