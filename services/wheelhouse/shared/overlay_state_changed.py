"""OverlayStateChangedEvent GUI -> Logic event schema (wh-9gkh5k).

Defines the GUI-to-Logic event that reports the numbered-overlay paint
outcome back to Logic during Phase 1.5 of the voice-element-clicking
feature (epic wh-l4h.1). The authoritative field spec lives in the v4
design doc:
``docs/plans/2026-05-28-voice-element-clicking-phase-1-5-design-v4.md``
under "New schemas (Phase 1.5 only)".

Lifecycle: after the GUI applies (or fails to apply, or clears) a
``paint_overlay`` / ``clear_overlay`` request, it reports the resulting
``state`` back to Logic as the ``overlay_state_changed`` action. Logic
validates inbound via ``safe_parse(OverlayStateChangedEvent.from_dict, ...)``
(wh-uf54), applies the generation check, then drives its overlay state
table. ``state`` is a closed set: ``"painted"`` (the overlay is up),
``"failed"`` (the paint could not be applied), or ``"cleared"`` (the
overlay was torn down).

``monitor_ids`` carries the monitors the overlay actually painted on (empty
tuple when none); ``snapshot_id`` echoes the painted snapshot when known, or
``None``. ``from_dict`` never lets a raw ``KeyError`` / ``TypeError`` /
``AttributeError`` escape -- every structural problem surfaces as
``OverlayStateChangedEventSchemaError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.schema_guard import reraise_as_schema_error


ACTION_NAME = "overlay_state_changed"


# Closed-set membership for the state field. Mirrors the _ALLOWED_STATUS
# pattern in show_numbered_overlay.py.
_ALLOWED_STATE = frozenset({"painted", "failed", "cleared"})


class OverlayStateChangedEventSchemaError(ValueError):
    """Raised by ``OverlayStateChangedEvent.from_dict`` on a bad payload.

    The Logic process should catch this via ``safe_parse`` and degrade
    gracefully (log + drop), per wh-uf54. ``from_dict`` never lets a raw
    ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.
    """


@dataclass(frozen=True)
class OverlayStateChangedEvent:
    """Structured GUI -> Logic overlay_state_changed event."""

    state: str
    overlay_session_id: int
    paint_generation: int
    monitor_ids: tuple[int, ...] = ()
    snapshot_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        ``monitor_ids`` is emitted as a list (JSON-friendly); ``snapshot_id``
        is emitted even when ``None``. The ``"action"`` key carries
        ``ACTION_NAME`` so the Logic dispatch can route it.
        """

        return {
            "action": ACTION_NAME,
            "state": self.state,
            "overlay_session_id": self.overlay_session_id,
            "paint_generation": self.paint_generation,
            "monitor_ids": list(self.monitor_ids),
            "snapshot_id": self.snapshot_id,
        }

    @classmethod
    @reraise_as_schema_error(OverlayStateChangedEventSchemaError)
    def from_dict(cls, payload: Any) -> "OverlayStateChangedEvent":
        """Parse and validate a wire-format dict.

        ``to_dict`` always emits every key, so ``from_dict`` requires all of
        them. Raises ``OverlayStateChangedEventSchemaError`` on any
        structural problem: not a mapping, missing or wrong ``"action"``, a
        missing / non-str ``"state"`` or one outside the closed set, missing /
        non-int ``"overlay_session_id"`` / ``"paint_generation"`` (``bool``
        excluded -- it is a subclass of ``int``), a ``"monitor_ids"`` that is
        not a builtin list/tuple of bool-excluded ints, or a ``"snapshot_id"``
        that is neither ``None`` nor a str.
        """

        if not isinstance(payload, Mapping):
            raise OverlayStateChangedEventSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "action" not in payload:
            raise OverlayStateChangedEventSchemaError(
                "payload missing required key 'action'"
            )
        if payload["action"] != ACTION_NAME:
            raise OverlayStateChangedEventSchemaError(
                f"payload action {payload['action']!r} does not match "
                f"{ACTION_NAME!r}"
            )

        if "state" not in payload:
            raise OverlayStateChangedEventSchemaError(
                "payload missing required field 'state'"
            )
        state = payload["state"]
        if not isinstance(state, str):
            raise OverlayStateChangedEventSchemaError(
                f"field 'state' must be a str, got {type(state).__name__}"
            )
        if state not in _ALLOWED_STATE:
            raise OverlayStateChangedEventSchemaError(
                f"field 'state' must be one of {sorted(_ALLOWED_STATE)}, "
                f"got {state!r}"
            )

        overlay_session_id = _require_int(payload, "overlay_session_id")
        paint_generation = _require_int(payload, "paint_generation")

        monitor_ids = _parse_monitor_ids(payload)

        if "snapshot_id" not in payload:
            raise OverlayStateChangedEventSchemaError(
                "payload missing required field 'snapshot_id'"
            )
        snapshot_id = payload["snapshot_id"]
        if snapshot_id is not None and not isinstance(snapshot_id, str):
            raise OverlayStateChangedEventSchemaError(
                "field 'snapshot_id' must be a str or None, "
                f"got {type(snapshot_id).__name__}"
            )

        return cls(
            state=state,
            overlay_session_id=overlay_session_id,
            paint_generation=paint_generation,
            monitor_ids=monitor_ids,
            snapshot_id=snapshot_id,
        )


def _require_int(payload: Mapping[str, Any], field_name: str) -> int:
    """Return ``payload[field_name]`` as an int, excluding ``bool``.

    Raises ``OverlayStateChangedEventSchemaError`` when the field is absent,
    not an int, or a ``bool`` (a subclass of ``int``).
    """

    if field_name not in payload:
        raise OverlayStateChangedEventSchemaError(
            f"payload missing required field {field_name!r}"
        )
    value = payload[field_name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise OverlayStateChangedEventSchemaError(
            f"field {field_name!r} must be an int, "
            f"got {type(value).__name__}"
        )
    return value


def _parse_monitor_ids(payload: Mapping[str, Any]) -> tuple[int, ...]:
    """Validate and normalize ``monitor_ids`` to a ``tuple[int, ...]``.

    Accepts the EXACT builtin ``list`` or ``tuple`` type of any length whose
    members are all bool-excluded ints (a JSON-bridged transport delivers a
    tuple as a list). The exact-type check (not ``isinstance``) fences out a
    hostile list subclass whose ``__len__`` / ``__getitem__`` / ``__iter__``
    raises, before any of those dunders runs (wh-9f3t.12.1). Mirrors
    ``walk_snapshot_serde._parse_bounds`` but with any length, not exactly 4.
    """

    if "monitor_ids" not in payload:
        raise OverlayStateChangedEventSchemaError(
            "payload missing required field 'monitor_ids'"
        )
    raw = payload["monitor_ids"]
    if type(raw) is not list and type(raw) is not tuple:
        raise OverlayStateChangedEventSchemaError(
            "field 'monitor_ids' must be a builtin list or tuple, "
            f"got {type(raw).__name__}"
        )
    for member in raw:
        if isinstance(member, bool) or not isinstance(member, int):
            raise OverlayStateChangedEventSchemaError(
                "field 'monitor_ids' contains a non-int member: "
                f"{type(member).__name__}"
            )
    return tuple(int(member) for member in raw)
