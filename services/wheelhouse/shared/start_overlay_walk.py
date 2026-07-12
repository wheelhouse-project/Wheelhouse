"""StartOverlayWalkResponse IPC schema (wh-n29v.37).

Defines the Input -> Logic reply for the ``start_overlay_walk`` action,
Phase 1.5 of the voice-element-clicking feature (epic wh-l4h.1). The
authoritative lifecycle lives in the v4 design doc:
``docs/plans/2026-05-28-voice-element-clicking-phase-1-5-design-v4.md``
under the "New schemas (Phase 1.5 only)" IPC-contract rows and section r2.10.

Lifecycle: Logic dispatches ``start_overlay_walk(scope="focused_window",
overlay_session_id, paint_generation, trace_id)`` to the Input process over
SharedMemory when the overlay state machine needs a FRESH walk of the focused
window (the standalone "show numbers" toggle, with NO prior ``click_element``
request to reuse). The Input handler walks the focused window from scratch,
numbers every interactive control 1..K in reading order, builds a plain-data
``WalkSnapshotSummary``, and replies with this schema, echoing
``overlay_session_id`` + ``paint_generation`` + ``trace_id`` verbatim so Logic
can drop a superseded walk's response by generation (design v4 "Supersession
generation").

Like ``ClickElementResponse`` and ``ShowNumberedOverlayResponse``, this is a
request-correlated Schema A response (handler-owned, correlated by
``request_id`` via the ``_HANDLES_OWN_RESPONSE`` machinery), not a type-routed
unsolicited event; ``status`` is the Schema A transport field, so this schema
carries no routing ``type`` key.

The status / outcome split (matches the extended ShowNumberedOverlayResponse
MECHANISM, r2.10)
==================================================================
* ``status: Literal["ok", "error"]`` is the Schema-A TRANSPORT envelope
  (matching ``ClickElementResponse``). It is NOT a feature-state duplicate.
* ``outcome: Literal["ok", "no_targets", "execution_failed", "error"]`` is the
  SINGLE AUTHORITATIVE feature-level result.

  The outcome set deliberately OMITS ``snapshot_expired`` (which
  ``ShowNumberedOverlayResponse`` carries): ``start_overlay_walk`` is ALWAYS a
  fresh walk with no input snapshot id, so it cannot expire one. The two
  schemas share the split MECHANISM but not the literal set, for that reason.

Mapping (the documented r2.10 contract):
  * ``outcome="ok"``   -> ``status="ok"``.
  * any NON-ok ``outcome`` (``no_targets`` / ``execution_failed``) still rides
    a transport ``status="ok"`` -- the handler RAN and reported a feature
    failure; the response is structurally trustworthy.
  * ``status="error"`` is reserved for a HANDLER-LEVEL crash/degrade where the
    ``outcome`` is unreliable; the never-raise handler maps an unexpected
    exception to ``status="error"`` + ``outcome="error"``.

The handler emits ONLY ``status`` in ``{"ok", "error"}``; the closed
membership set below intentionally does NOT include the ``not_implemented``
literal that the stub ``ShowNumberedOverlayResponse`` carries -- there is no
stub phase for this REAL handler.

Transport: the Input Process puts a dict produced by
``StartOverlayWalkResponse.to_dict()`` onto the response queue. The Logic
Process calls ``StartOverlayWalkResponse.from_dict()`` and catches
``StartOverlayWalkResponseSchemaError`` for graceful degradation (log + drop)
on a malformed payload, per wh-uf54. ``from_dict`` never lets a raw
``KeyError`` / ``TypeError`` / ``AttributeError`` escape.

Field meanings:
  * ``status`` -- Schema A transport, one of ``"ok"`` / ``"error"``. Closed.
  * ``outcome`` -- authoritative feature result. Closed set
    ``{"ok", "no_targets", "execution_failed", "error"}``.
  * ``reason`` -- open tag (a walk-time reason such as
    ``walk_deadline_exceeded``, or a handler-level error string), or ``None``.
  * ``snapshot_id`` -- the fresh walk's snapshot id, or ``None`` when no
    snapshot was produced (a handler-level crash before the walk).
  * ``snapshot_summary`` -- the plain-data ``WalkSnapshotSummary`` the GUI
    paints, or ``None``. Serialized to / from JSON-friendly primitives by
    ``shared.walk_snapshot_serde``. A ``no_targets`` outcome may carry either
    an empty-items summary or ``None``.
  * ``trace_id`` -- the Logic-generated correlation id, echoed verbatim.
  * ``overlay_session_id`` -- the overlay session this walk belongs to, echoed
    verbatim for the generation/supersession check.
  * ``paint_generation`` -- the paint generation within the session, echoed
    verbatim for the generation/supersession check.
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


class StartOverlayWalkResponseSchemaError(ValueError):
    """Raised by ``StartOverlayWalkResponse.from_dict`` on a bad payload.

    The Logic process should catch this and degrade gracefully
    (log + drop), per wh-uf54. ``from_dict`` never lets a raw
    ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.
    """


# Closed-set membership for the Schema A transport status. Only ``"ok"`` and
# ``"error"`` are EMITTED by the real handler; ``not_implemented`` is NOT a
# member here (unlike the stub ShowNumberedOverlayResponse) because this
# handler ships real from the start.
_ALLOWED_STATUS = frozenset({"ok", "error"})

# Closed-set membership for the authoritative feature outcome. This set
# deliberately OMITS ``snapshot_expired`` -- a fresh walk has no input
# snapshot to expire (design v4 IPC-contract row for StartOverlayWalkResponse).
_ALLOWED_OUTCOME = frozenset({"ok", "no_targets", "execution_failed", "error"})


@dataclass(frozen=True)
class StartOverlayWalkResponse:
    """Structured Input -> Logic reply for the start_overlay_walk action."""

    status: str
    outcome: str
    reason: str | None
    snapshot_id: str | None
    snapshot_summary: WalkSnapshotSummary | None
    trace_id: str
    overlay_session_id: int
    paint_generation: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        ``snapshot_summary`` is serialized to a nested dict of JSON-friendly
        primitives, or ``None`` when absent.
        """

        return {
            "status": self.status,
            "outcome": self.outcome,
            "reason": self.reason,
            "snapshot_id": self.snapshot_id,
            "snapshot_summary": summary_to_dict(self.snapshot_summary),
            "trace_id": self.trace_id,
            "overlay_session_id": self.overlay_session_id,
            "paint_generation": self.paint_generation,
        }

    @classmethod
    @reraise_as_schema_error(StartOverlayWalkResponseSchemaError)
    def from_dict(cls, payload: Any) -> "StartOverlayWalkResponse":
        """Parse and validate a wire-format dict.

        Raises ``StartOverlayWalkResponseSchemaError`` on any structural
        problem: not a mapping, a missing required field, a wrong field type,
        a ``status`` or ``outcome`` value outside its closed set, or a
        malformed nested ``snapshot_summary``. Never lets a raw
        ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.
        """

        if not isinstance(payload, Mapping):
            raise StartOverlayWalkResponseSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        status = _require_closed_str(
            payload, "status", _ALLOWED_STATUS
        )
        outcome = _require_closed_str(
            payload, "outcome", _ALLOWED_OUTCOME
        )
        reason = _require_optional_str(payload, "reason")
        snapshot_id = _require_optional_str(payload, "snapshot_id")

        if "snapshot_summary" not in payload:
            raise StartOverlayWalkResponseSchemaError(
                "payload missing required field 'snapshot_summary'"
            )
        snapshot_summary = summary_from_dict(
            payload["snapshot_summary"],
            StartOverlayWalkResponseSchemaError,
        )

        trace_id = _require_str(payload, "trace_id")
        overlay_session_id = _require_int(payload, "overlay_session_id")
        paint_generation = _require_int(payload, "paint_generation")

        _validate_cross_field(
            status=status,
            outcome=outcome,
            snapshot_id=snapshot_id,
            snapshot_summary=snapshot_summary,
        )

        return cls(
            status=status,
            outcome=outcome,
            reason=reason,
            snapshot_id=snapshot_id,
            snapshot_summary=snapshot_summary,
            trace_id=trace_id,
            overlay_session_id=overlay_session_id,
            paint_generation=paint_generation,
        )


def _validate_cross_field(
    *,
    status: str,
    outcome: str,
    snapshot_id: str | None,
    snapshot_summary: WalkSnapshotSummary | None,
) -> None:
    """Enforce the documented r2.10 cross-field invariants (reviewer_1 39.2).

    The per-field parsing above validates ``status`` / ``outcome`` /
    ``snapshot_id`` / ``snapshot_summary`` in ISOLATION, which lets
    field-combination-illegal payloads through. The handler emits ONLY these
    combinations:

      * ``status="ok"``,    ``outcome="ok"``,              snapshot_id set, summary set
      * ``status="ok"``,    ``outcome="no_targets"``,      snapshot_id set, empty-items summary set
      * ``status="ok"``,    ``outcome="execution_failed"``, snapshot_id=None, summary=None
      * ``status="error"``, ``outcome="error"``,           snapshot_id=None, summary=None

    so a payload that violates one of the five invariants below could not have
    come from the handler. Rejecting it here (with the schema's typed error)
    keeps a malformed Input payload from crossing the graceful-degrade boundary
    as if it were trustworthy -- otherwise Logic cannot tell a transport failure
    from a feature failure, or safely key the retained summary for "click N".

    Raises ``StartOverlayWalkResponseSchemaError`` on a violation.
    """
    # (a) The transport "error" status and the feature "error" outcome are
    #     paired: one is "error" iff the other is. A handler-level crash sets
    #     BOTH; every non-crash response sets NEITHER. A one-sided "error" means
    #     a corrupted payload (status=ok+outcome=error, or status=error+outcome
    #     in {ok, no_targets, execution_failed}).
    if (status == "error") != (outcome == "error"):
        raise StartOverlayWalkResponseSchemaError(
            "status/outcome 'error' mismatch: status="
            f"{status!r} outcome={outcome!r} (status is 'error' iff outcome is)"
        )

    # (b) A successful walk (outcome="ok") MUST carry a non-empty snapshot_id AND
    #     a summary -- the snapshot the overlay paints and a later
    #     click_snapshot_item keys on. The presence rule is scoped to "ok" ONLY:
    #     the schema docstring documents that "no_targets" may carry an
    #     empty-items summary OR None, so it is deliberately NOT constrained here
    #     (matching the finding's "require ok to carry ..." wording). An empty
    #     string snapshot_id parses as a str but is not a usable id, so ``not
    #     snapshot_id`` rejects both None and "".
    if outcome == "ok":
        if not snapshot_id:
            raise StartOverlayWalkResponseSchemaError(
                "outcome='ok' requires a non-empty snapshot_id, got "
                f"{snapshot_id!r}"
            )
        if snapshot_summary is None:
            raise StartOverlayWalkResponseSchemaError(
                "outcome='ok' requires a non-None snapshot_summary"
            )

    # (c) When BOTH the top-level snapshot_id and a summary are present, they
    #     must name the SAME snapshot -- Logic keys the retained summary by the
    #     top-level id, so a disagreement would mis-key "click N". This also
    #     covers no_targets-with-summary (both present) without constraining the
    #     no_targets-with-None case.
    if snapshot_id is not None and snapshot_summary is not None:
        if snapshot_summary.snapshot_id != snapshot_id:
            raise StartOverlayWalkResponseSchemaError(
                "snapshot_id disagreement: top-level "
                f"{snapshot_id!r} != snapshot_summary.snapshot_id "
                f"{snapshot_summary.snapshot_id!r}"
            )

    # (d) A FAILURE outcome (execution_failed / error) carries NO snapshot at
    #     all -- the handler emits both ONLY with snapshot_id=None AND
    #     summary=None (no walk produced a paintable snapshot). A
    #     snapshot-bearing failure could not have come from the handler;
    #     accepting it would let Logic cache or correlate a snapshot for a
    #     response that says no valid walk exists (reviewer_1 finding 39.3).
    #     no_targets is deliberately excluded here -- it is a successful walk of
    #     an empty window and keeps its documented snapshot-or-None flexibility.
    if outcome in ("execution_failed", "error"):
        if snapshot_id is not None:
            raise StartOverlayWalkResponseSchemaError(
                f"outcome={outcome!r} must carry snapshot_id=None, got "
                f"{snapshot_id!r}"
            )
        if snapshot_summary is not None:
            raise StartOverlayWalkResponseSchemaError(
                f"outcome={outcome!r} must carry snapshot_summary=None"
            )

    # (e) no_targets means the walk found nothing clickable, so a summary WITH
    #     items contradicts the outcome (wh-overlay-no-targets-summary-guard).
    #     The handler emits no_targets exclusively with an empty-items summary
    #     (element_finder.overlay_walk sets no_targets iff matches is empty),
    #     so a populated-items no_targets could not have come from it. The
    #     empty-items-summary-OR-None flexibility documented above is kept:
    #     only the populated-items combination is rejected.
    if (
        outcome == "no_targets"
        and snapshot_summary is not None
        and snapshot_summary.items
    ):
        raise StartOverlayWalkResponseSchemaError(
            "outcome='no_targets' must not carry a populated-items "
            f"snapshot_summary, got {len(snapshot_summary.items)} item(s)"
        )


def _require_str(payload: Mapping[Any, Any], key: str) -> str:
    if key not in payload:
        raise StartOverlayWalkResponseSchemaError(
            f"payload missing required field {key!r}"
        )
    value = payload[key]
    if not isinstance(value, str):
        raise StartOverlayWalkResponseSchemaError(
            f"field {key!r} must be a str, got {type(value).__name__}"
        )
    return value


def _require_closed_str(
    payload: Mapping[Any, Any], key: str, allowed: frozenset[str]
) -> str:
    value = _require_str(payload, key)
    if value not in allowed:
        raise StartOverlayWalkResponseSchemaError(
            f"field {key!r} must be one of {sorted(allowed)}, got {value!r}"
        )
    return value


def _require_optional_str(payload: Mapping[Any, Any], key: str) -> str | None:
    if key not in payload:
        raise StartOverlayWalkResponseSchemaError(
            f"payload missing required field {key!r}"
        )
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise StartOverlayWalkResponseSchemaError(
            f"field {key!r} must be a str or None, got {type(value).__name__}"
        )
    return value


def _require_int(payload: Mapping[Any, Any], key: str) -> int:
    if key not in payload:
        raise StartOverlayWalkResponseSchemaError(
            f"payload missing required field {key!r}"
        )
    value = payload[key]
    # bool is a subclass of int; exclude it explicitly so an echoed True is
    # not silently read as 1 (the generation fields are real counts).
    if isinstance(value, bool) or not isinstance(value, int):
        raise StartOverlayWalkResponseSchemaError(
            f"field {key!r} must be an int, got {type(value).__name__}"
        )
    return value
