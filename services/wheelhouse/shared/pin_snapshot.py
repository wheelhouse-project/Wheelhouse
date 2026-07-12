"""PinSnapshotResponse IPC schema (wh-n29v.41).

Defines the small Input -> Logic acknowledgement for the ``pin_snapshot`` and
``unpin_snapshot`` actions, Phase 1.5 of the voice-element-clicking feature
(epic wh-l4h.1). The authoritative lifecycle lives in the v4 design doc:
``docs/plans/2026-05-28-voice-element-clicking-phase-1-5-design-v4.md`` under
the "New schemas (Phase 1.5 only)" IPC-contract rows (the
``pin_snapshot`` / ``unpin_snapshot`` / ``PinSnapshotResponse`` rows near line
428), the supersession-generation section r1c.2, and the multi-snapshot-store
pin-transport section r1c.1.

Lifecycle: Logic owns the active-overlay pin. It dispatches
``pin_snapshot(overlay_session_id, snapshot_id, paint_generation)`` to the
Input process when it dispatches the ``paint_overlay`` that displays a
snapshot, and ``unpin_snapshot(overlay_session_id, snapshot_id)`` on every
unpin trigger (hide-numbers, refresh replacement, focused-window-change
replacement, the ``paint_in_flight`` / ``walk_in_flight`` restart, the
``error -> closed`` recovery, and the mic-resume identity-mismatch
invalidation). The Input handlers drive the multi-snapshot store's
``ElementFinder.pin`` / ``ElementFinder.unpin`` and reply with this schema.

Logic does NOT block the overlay paint on this ack (it is defence-in-depth
layered over LRU recency + TTL). The ack is consumed for bookkeeping; a
reported pin failure is logged and re-dispatched. Each handler emits exactly
one ``PinSnapshotResponse`` so the Logic-side awaiting Future resolves and the
``app.py`` demuxer does not leak. The ONE exception is a dead response queue:
if ``response_queue.put`` itself raises (the Logic process has crashed, or the
queue is closed), the handler logs the failure and returns having emitted ZERO
responses. There is no second channel to deliver on, and the now-orphaned
Logic Future is already covered by Logic's own timeout, so this is acceptable.
The handler never raises and never emits two responses; "exactly one response"
holds in every case except a response queue that is itself unavailable.

Like ``StartOverlayWalkResponse`` and ``ClickElementResponse``, this is a
request-correlated Schema A response (handler-owned, correlated by
``request_id`` via the ``_HANDLES_OWN_RESPONSE`` machinery), not a type-routed
unsolicited event; ``status`` is the Schema A transport field, so this schema
carries no routing ``type`` key.

Status semantics
================
``status: Literal["ok", "error"]`` is the Schema-A TRANSPORT envelope
(matching ``ClickElementResponse`` / ``StartOverlayWalkResponse``):

* ``status="ok"`` -- the handler RAN and reported a store result. The ``pinned``
  bool plus the ``reason`` carry the authoritative outcome: ``pinned=True`` on a
  successful pin; ``pinned=False`` on a successful unpin, on an unknown
  ``snapshot_id``, or on a stale-generation rejection (the ``reason`` names which).
* ``status="error"`` -- a HANDLER-LEVEL crash/degrade where the result is
  unreliable; the never-raise handler maps an unexpected exception to
  ``status="error"`` + ``pinned=False`` + a ``reason`` tag.

There is NO separate ``outcome`` field (unlike ``StartOverlayWalkResponse``):
this ack carries no walk result and no snapshot summary, only the pin
flag and the echoed identity, so the ``pinned`` bool plus ``reason`` are the
authoritative feature signal. The status set is closed to ``{"ok", "error"}``;
no stub ``not_implemented`` literal -- this handler ships real from the start.

Transport: the Input Process puts a dict produced by
``PinSnapshotResponse.to_dict()`` onto the response queue. The Logic Process
calls ``PinSnapshotResponse.from_dict()`` and catches
``PinSnapshotResponseSchemaError`` for graceful degradation (log + drop) on a
malformed payload, per wh-uf54. ``from_dict`` never lets a raw ``KeyError`` /
``TypeError`` / ``AttributeError`` escape.

Field meanings:
  * ``status`` -- Schema A transport, one of ``"ok"`` / ``"error"``. Closed.
  * ``reason`` -- open tag, or ``None`` on a clean success. The set is OPEN
    (Logic must tolerate an unrecognized tag), but the tags the handlers
    actually emit are: ``unknown_snapshot`` (pin or unpin: the snapshot_id is
    not in the store), ``stale_generation`` (pin: a same-session
    strictly-older paint_generation), ``stale_session`` (pin: an overlay
    session older than the latest seen), ``disabled_by_config`` (the overlay
    is disabled), ``invalid_request`` (malformed IPC field types, rejected
    before any store or watermark mutation), and ``unexpected_error`` (a
    handler-level crash mapped to ``status="error"``).
  * ``overlay_session_id`` -- the overlay session this pin belongs to, echoed
    verbatim so Logic can correlate the ack.
  * ``snapshot_id`` -- the snapshot the pin/unpin targeted, echoed verbatim.
  * ``pinned`` -- the resulting pin state: ``True`` only on a successful pin;
    ``False`` on a successful unpin, a no-op (unknown id), a stale rejection,
    or a handler-level error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.schema_guard import reraise_as_schema_error


class PinSnapshotResponseSchemaError(ValueError):
    """Raised by ``PinSnapshotResponse.from_dict`` on a bad payload.

    The Logic process should catch this and degrade gracefully
    (log + drop), per wh-uf54. ``from_dict`` never lets a raw
    ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.
    """


# Closed-set membership for the Schema A transport status. Only ``"ok"`` and
# ``"error"`` are EMITTED by the handlers; there is no stub phase for these
# REAL handlers, so ``not_implemented`` is NOT a member.
_ALLOWED_STATUS = frozenset({"ok", "error"})


@dataclass(frozen=True)
class PinSnapshotResponse:
    """Structured Input -> Logic ack for pin_snapshot / unpin_snapshot."""

    status: str
    reason: str | None
    overlay_session_id: int
    snapshot_id: str
    pinned: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict (JSON-friendly primitives)."""

        return {
            "status": self.status,
            "reason": self.reason,
            "overlay_session_id": self.overlay_session_id,
            "snapshot_id": self.snapshot_id,
            "pinned": self.pinned,
        }

    @classmethod
    @reraise_as_schema_error(PinSnapshotResponseSchemaError)
    def from_dict(cls, payload: Any) -> "PinSnapshotResponse":
        """Parse and validate a wire-format dict.

        Raises ``PinSnapshotResponseSchemaError`` on any structural problem:
        not a mapping, a missing required field, a wrong field type, a
        ``status`` value outside its closed set, or a non-bool ``pinned``.
        Never lets a raw ``KeyError`` / ``TypeError`` / ``AttributeError``
        escape.
        """

        if not isinstance(payload, Mapping):
            raise PinSnapshotResponseSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        status = _require_closed_str(payload, "status", _ALLOWED_STATUS)
        reason = _require_optional_str(payload, "reason")
        overlay_session_id = _require_int(payload, "overlay_session_id")
        snapshot_id = _require_str(payload, "snapshot_id")
        pinned = _require_bool(payload, "pinned")

        return cls(
            status=status,
            reason=reason,
            overlay_session_id=overlay_session_id,
            snapshot_id=snapshot_id,
            pinned=pinned,
        )


def _require_str(payload: Mapping[Any, Any], key: str) -> str:
    if key not in payload:
        raise PinSnapshotResponseSchemaError(
            f"payload missing required field {key!r}"
        )
    value = payload[key]
    if not isinstance(value, str):
        raise PinSnapshotResponseSchemaError(
            f"field {key!r} must be a str, got {type(value).__name__}"
        )
    return value


def _require_closed_str(
    payload: Mapping[Any, Any], key: str, allowed: frozenset[str]
) -> str:
    value = _require_str(payload, key)
    if value not in allowed:
        raise PinSnapshotResponseSchemaError(
            f"field {key!r} must be one of {sorted(allowed)}, got {value!r}"
        )
    return value


def _require_optional_str(payload: Mapping[Any, Any], key: str) -> str | None:
    if key not in payload:
        raise PinSnapshotResponseSchemaError(
            f"payload missing required field {key!r}"
        )
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise PinSnapshotResponseSchemaError(
            f"field {key!r} must be a str or None, got {type(value).__name__}"
        )
    return value


def _require_int(payload: Mapping[Any, Any], key: str) -> int:
    if key not in payload:
        raise PinSnapshotResponseSchemaError(
            f"payload missing required field {key!r}"
        )
    value = payload[key]
    # bool is a subclass of int; exclude it explicitly so an echoed True is
    # not silently read as 1 (overlay_session_id is a real count).
    if isinstance(value, bool) or not isinstance(value, int):
        raise PinSnapshotResponseSchemaError(
            f"field {key!r} must be an int, got {type(value).__name__}"
        )
    return value


def _require_bool(payload: Mapping[Any, Any], key: str) -> bool:
    if key not in payload:
        raise PinSnapshotResponseSchemaError(
            f"payload missing required field {key!r}"
        )
    value = payload[key]
    # Strictly bool: an int (even 1/0) is rejected so a corrupted payload can
    # never be silently read as a pin state.
    if not isinstance(value, bool):
        raise PinSnapshotResponseSchemaError(
            f"field {key!r} must be a bool, got {type(value).__name__}"
        )
    return value
