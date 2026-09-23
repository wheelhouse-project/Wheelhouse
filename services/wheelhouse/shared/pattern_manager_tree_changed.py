"""pattern_manager_tree_changed GUI -> Logic event schema.

Bead wh-overlay-rewalk-after-filter, contract point C5. When the Pattern
Manager's tree changes what a UI Automation walk of the dialog would return
-- the filter box hiding rows, or a repopulate after a pattern or category
was added, removed, renamed or moved -- the numbered overlay's badges go
stale: they were painted from a walk taken before the change and nothing
re-walks afterwards.

Measurement on this bead (STAGE MEASURE DONE, probe scripts committed at
58955e22) showed the window announces NOTHING when the filter hides rows:
zero UIA StructureChanged events and zero WinEvents 0x8000..0x8004, against
a probe control of 150 of each over a Chromium page that adds and removes a
control every 200 ms. No Logic-side hook can see this change, so the GUI
process -- which knows its own model changed -- tells Logic directly. Logic
maps the event onto the existing FOCUS_CHANGE effect through the existing
foreground debouncer, the same way a menu pop-up event is mapped.

WHY THE ACTION NAME CARRIES NO ``pm_`` PREFIX. The Logic command listener
routes every action whose string starts with ``"pm_"`` to
``_handle_pattern_manager_action`` BEFORE it consults the handler table
(``services/wheelhouse/main.py:11249``). A ``pm_``-prefixed name would
therefore never reach this event's handler; it would land in the Pattern
Manager request/response path instead. The name is
``pattern_manager_tree_changed`` for that reason and must stay that way.

Transport: the dialog emits its ``tree_changed`` Qt signal carrying the dict
produced by :meth:`PatternManagerTreeChangedEvent.to_dict` -- shape
``{"action": "pattern_manager_tree_changed", "hwnd": ..., "sequence": ...}``
-- and the GUI manager puts it onto the existing commands_to_logic_queue.
``LogicController._listen_for_gui_commands`` routes the action via its
handler map; the handler validates the payload with :func:`safe_parse`, so a
malformed payload is logged and dropped rather than crashing the listener.

``hwnd`` is the dialog's TOP-LEVEL window handle, so Logic can compare it
against the window the overlay was painted over; ``0`` never travels on the
wire because ``0`` is the Logic side's "no window" value. ``sequence`` is a
per-dialog monotonic counter bumped before each send, so Logic can drop an
event that arrives out of order behind a newer one for the same window; the
GUI bumps before it sends, so the first event is ``1``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.schema_guard import reraise_as_schema_error


ACTION_NAME = "pattern_manager_tree_changed"


class PatternManagerTreeChangedSchemaError(ValueError):
    """Raised by ``PatternManagerTreeChangedEvent.from_dict`` on a bad payload.

    The Logic handler catches this via ``safe_parse`` and degrades gracefully
    (log + drop). ``from_dict`` never lets a raw ``KeyError`` / ``TypeError``
    / ``AttributeError`` escape.
    """


@dataclass(frozen=True)
class PatternManagerTreeChangedEvent:
    """Structured payload of a pattern_manager_tree_changed GUI -> Logic event."""

    hwnd: int
    sequence: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        The returned dict is the action payload the GUI puts onto
        commands_to_logic_queue; the ``"action"`` key carries ``ACTION_NAME``
        so the dispatch in ``_listen_for_gui_commands`` can route it.
        """

        return {
            "action": ACTION_NAME,
            "hwnd": self.hwnd,
            "sequence": self.sequence,
        }

    @classmethod
    @reraise_as_schema_error(PatternManagerTreeChangedSchemaError)
    def from_dict(cls, payload: Any) -> "PatternManagerTreeChangedEvent":
        """Parse and validate a wire-format dict.

        Raises ``PatternManagerTreeChangedSchemaError`` on any structural
        problem: not a mapping, missing or wrong ``"action"``, missing or
        non-int ``"hwnd"`` / ``"sequence"`` (``bool`` excluded -- it is a
        subclass of ``int``), an ``hwnd`` that is not positive (``0`` is the
        Logic side's "no window" value and must not arrive on the wire), or a
        ``sequence`` below 1 (the GUI bumps its counter before it sends).
        """

        if not isinstance(payload, Mapping):
            raise PatternManagerTreeChangedSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "action" not in payload:
            raise PatternManagerTreeChangedSchemaError(
                "payload missing required key 'action'"
            )
        if payload["action"] != ACTION_NAME:
            raise PatternManagerTreeChangedSchemaError(
                f"payload action {payload['action']!r} does not match "
                f"{ACTION_NAME!r}"
            )

        if "hwnd" not in payload:
            raise PatternManagerTreeChangedSchemaError(
                "payload missing required field 'hwnd'"
            )
        hwnd = payload["hwnd"]
        # bool is a subclass of int; exclude it explicitly.
        if isinstance(hwnd, bool) or not isinstance(hwnd, int):
            raise PatternManagerTreeChangedSchemaError(
                f"field 'hwnd' must be an int, got {type(hwnd).__name__}"
            )
        if hwnd <= 0:
            raise PatternManagerTreeChangedSchemaError(
                "field 'hwnd' must be > 0 (0 is the Logic side's 'no window' "
                f"value), got {hwnd}"
            )

        if "sequence" not in payload:
            raise PatternManagerTreeChangedSchemaError(
                "payload missing required field 'sequence'"
            )
        sequence = payload["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise PatternManagerTreeChangedSchemaError(
                "field 'sequence' must be an int, "
                f"got {type(sequence).__name__}"
            )
        if sequence < 1:
            raise PatternManagerTreeChangedSchemaError(
                "field 'sequence' must be >= 1 (the GUI bumps its counter "
                f"before it sends), got {sequence}"
            )

        return cls(hwnd=hwnd, sequence=sequence)
