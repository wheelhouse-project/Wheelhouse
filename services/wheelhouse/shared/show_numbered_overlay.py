"""ShowNumberedOverlayResponse IPC schema (wh-jfavj / wh-n29v.79).

Defines the Input -> Logic reply for the ``show_numbered_overlay`` action,
Phase 1.5 of the voice-element-clicking feature (epic wh-l4h.1). The
authoritative lifecycle lives in the v4 design doc:
``docs/plans/2026-05-28-voice-element-clicking-phase-1-5-design-v4.md`` under
the r2.10 status/outcome split and the "New schemas (Phase 1.5 only)"
IPC-contract rows (lines 409-410, 416).

Lifecycle: Logic sends ``show_numbered_overlay(snapshot_id)`` to the Input
process over SharedMemory; the Input handler looks up the retained walk
snapshot (an EXISTING snapshot id, NOT a fresh walk), re-runs pre-display
checks, builds a fresh ``WalkSnapshotSummary``, and replies with this schema.
Logic forwards the summary to the GUI as a ``paint_numbered_overlay`` action
and retains a copy keyed by ``snapshot_id`` so a later
``snapshot_item_clicked`` event can resolve a display number to an item.

Like ``ClickElementResponse`` and ``StartOverlayWalkResponse``, this is a
request-correlated Schema A response (handler-owned, correlated by
``request_id`` via the ``_HANDLES_OWN_RESPONSE`` machinery), not a type-routed
unsolicited event; ``status`` is the Schema A transport field, so this schema
carries no routing ``type`` key.

The status / outcome split (r2.10)
==================================
* ``status`` is the Schema-A TRANSPORT envelope. It is NOT a feature-state
  duplicate.
* ``outcome`` is the SINGLE AUTHORITATIVE feature-level result; ``from_dict``
  treats it as authoritative.

  The outcome closed set is
  ``{"ok", "snapshot_expired", "no_targets", "execution_failed", "error"}``.
  It INCLUDES ``snapshot_expired`` (which the sibling
  ``StartOverlayWalkResponse`` deliberately OMITS): ``start_overlay_walk`` is
  ALWAYS a fresh walk with no input snapshot to expire, whereas
  ``show_numbered_overlay`` re-uses an EXISTING snapshot id, so it CAN find
  that id stale/gone (or a foreground-identity mismatch) and report
  ``snapshot_expired`` (design v4 line 416). The two schemas share the split
  MECHANISM but not the literal set, for that reason.

Mapping (the documented r2.10 contract):
  * ``outcome="ok"``   -> ``status="ok"``.
  * any NON-ok ``outcome`` (``snapshot_expired`` / ``no_targets`` /
    ``execution_failed``) still rides a transport ``status="ok"`` -- the
    handler RAN and reported a feature failure; the response is structurally
    trustworthy.
  * ``status="error"`` is reserved for a HANDLER-LEVEL crash/degrade where the
    ``outcome`` is unreliable; the never-raise handler maps an unexpected
    exception to ``status="error"`` + ``outcome="error"``.

The REAL handler emits ONLY ``status`` in ``{"ok", "error"}`` (the documented,
emitted set). The closed membership set used for PARSING below ALSO accepts
``"not_implemented"``: that literal is NOT part of the real handler's emitted
status set -- it is retained ONLY as a defensive parse literal for the
SURVIVING Phase 1.5 stub (``UIActionHandler.show_numbered_overlay``), which
still emits ``status="not_implemented"`` with ``outcome="execution_failed"``
and no snapshot until the real walk lookup ships (wh-cy670k). A
``not_implemented`` payload therefore parses cleanly; its envelope satisfies
the cross-field rules (a) and (d) below.

Transport: the Input Process puts a dict produced by
``ShowNumberedOverlayResponse.to_dict()`` onto the response queue. The Logic
Process calls ``ShowNumberedOverlayResponse.from_dict()`` and catches
``ShowNumberedOverlayResponseSchemaError`` for graceful degradation
(log + drop) on a malformed payload, per wh-uf54. ``from_dict`` never lets a
raw ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.

Field meanings:
  * ``status`` -- Schema A transport. Real handler emits ``"ok"`` / ``"error"``;
    parse-accepts ``"not_implemented"`` (stub-only). Closed.
  * ``outcome`` -- authoritative feature result. Closed set
    ``{"ok", "snapshot_expired", "no_targets", "execution_failed", "error"}``.
  * ``reason`` -- open tag (a walk-time reason, a stale-snapshot tag such as
    ``stale_snapshot_id``, or a handler-level error string), or ``None``.
  * ``snapshot_id`` -- the painted walk's snapshot id, or ``None`` when no
    snapshot is painted (every failure outcome, or a handler-level crash).
  * ``snapshot_summary`` -- the plain-data ``WalkSnapshotSummary`` the GUI
    paints, or ``None``. Serialized to / from JSON-friendly primitives by
    ``shared.walk_snapshot_serde``. A ``no_targets`` outcome may carry either
    an empty-items summary or ``None``.
  * ``trace_id`` -- the Logic-generated correlation id, echoed verbatim.
  * ``overlay_session_id`` -- the overlay session this paint belongs to, echoed
    verbatim for the generation/supersession check.
  * ``paint_generation`` -- the paint generation within the session, echoed
    verbatim for the generation/supersession check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ui.element_types import WalkSnapshotSummary
from services.wheelhouse.shared.walk_snapshot_serde import (
    summary_from_dict,
    summary_to_dict,
)


class ShowNumberedOverlayResponseSchemaError(ValueError):
    """Raised by ``ShowNumberedOverlayResponse.from_dict`` on a bad payload.

    The Logic process should catch this and degrade gracefully
    (log + drop), per wh-uf54. ``from_dict`` never lets a raw
    ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.
    """


# Closed-set membership for the Schema A transport status used when PARSING.
# The REAL handler emits ONLY ``"ok"`` / ``"error"``; ``"not_implemented"`` is
# retained here as a defensive parse literal for the surviving Phase 1.5 stub
# (UIActionHandler.show_numbered_overlay), which still emits it until the real
# walk lookup ships (wh-cy670k). See the module docstring.
_ALLOWED_STATUS = frozenset({"ok", "error", "not_implemented"})

# Closed-set membership for the authoritative feature outcome. This set
# INCLUDES ``snapshot_expired`` (unlike the sibling StartOverlayWalkResponse):
# show_numbered_overlay re-uses an EXISTING snapshot id, so it can expire one
# (design v4 line 416).
_ALLOWED_OUTCOME = frozenset(
    {"ok", "snapshot_expired", "no_targets", "execution_failed", "error"}
)

# Failure outcomes that carry NO paintable snapshot (cross-field rule (d)).
# ``snapshot_expired`` joins ``execution_failed`` and ``error`` because a
# stale/gone snapshot has nothing to paint. ``no_targets`` is deliberately
# EXCLUDED: it is a successful walk of an empty window and keeps the
# snapshot-or-None flexibility.
_NO_SNAPSHOT_OUTCOMES = frozenset(
    {"snapshot_expired", "execution_failed", "error"}
)


@dataclass(frozen=True)
class ShowNumberedOverlayResponse:
    """Structured Input -> Logic reply for the show_numbered_overlay action."""

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
    def from_dict(cls, payload: Any) -> "ShowNumberedOverlayResponse":
        """Parse and validate a wire-format dict.

        Raises ``ShowNumberedOverlayResponseSchemaError`` on any structural
        problem: not a mapping, a missing required field, a wrong field type,
        a ``status`` or ``outcome`` value outside its closed set, a malformed
        nested ``snapshot_summary``, or a cross-field-illegal combination.
        Never lets a raw ``KeyError`` / ``TypeError`` / ``AttributeError``
        escape.
        """

        if not isinstance(payload, Mapping):
            raise ShowNumberedOverlayResponseSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        # wh-n29v.81.1: belt-and-suspenders for the wh-uf54 boundary. The
        # _require_* helpers and summary_from_dict access the mapping via
        # ``key in payload`` / ``payload[key]``; a hostile Mapping subclass
        # whose __contains__ or __getitem__ raises AttributeError / TypeError
        # passes the isinstance gate above and would otherwise bubble that RAW
        # exception, violating from_dict's promise that ONLY the typed
        # ShowNumberedOverlayResponseSchemaError escapes. Wrapping the WHOLE
        # parse body (including the nested summary_from_dict call) converts
        # those to the typed error, so walk_snapshot_serde.py need not change.
        # The already-typed schema error is a ValueError, NOT in the caught
        # tuple, so existing typed errors pass through unchanged.
        try:
            status = _require_closed_str(payload, "status", _ALLOWED_STATUS)
            outcome = _require_closed_str(payload, "outcome", _ALLOWED_OUTCOME)
            reason = _require_optional_str(payload, "reason")
            snapshot_id = _require_optional_str(payload, "snapshot_id")

            if "snapshot_summary" not in payload:
                raise ShowNumberedOverlayResponseSchemaError(
                    "payload missing required field 'snapshot_summary'"
                )
            snapshot_summary = summary_from_dict(
                payload["snapshot_summary"],
                ShowNumberedOverlayResponseSchemaError,
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
        except (KeyError, TypeError, AttributeError) as exc:
            raise ShowNumberedOverlayResponseSchemaError(
                f"malformed payload: {exc!r}"
            ) from exc


def _validate_cross_field(
    *,
    status: str,
    outcome: str,
    snapshot_id: str | None,
    snapshot_summary: WalkSnapshotSummary | None,
) -> None:
    """Enforce the documented r2.10 cross-field invariants.

    The per-field parsing above validates ``status`` / ``outcome`` /
    ``snapshot_id`` / ``snapshot_summary`` in ISOLATION, which lets
    field-combination-illegal payloads through. The handler (and the surviving
    stub) emit ONLY these combinations:

      * ``status="ok"``,    ``outcome="ok"``,               snapshot_id set, summary set
      * ``status="ok"``,    ``outcome="no_targets"``,       snapshot_id set OR None, empty-items summary OR None
      * ``status="ok"``,    ``outcome="snapshot_expired"``, snapshot_id=None, summary=None
      * ``status="ok"``,    ``outcome="execution_failed"``, snapshot_id=None, summary=None
      * ``status="error"``, ``outcome="error"``,            snapshot_id=None, summary=None
      * ``status="not_implemented"``, ``outcome="execution_failed"``, snapshot_id=None, summary=None  (stub)

    so a payload that violates one of the invariants below could not have come
    from the handler. Rejecting it here (with the schema's typed error) keeps a
    malformed Input payload from crossing the graceful-degrade boundary as if
    it were trustworthy -- otherwise Logic cannot tell a transport failure from
    a feature failure, or safely key the retained summary for "click N".

    Raises ``ShowNumberedOverlayResponseSchemaError`` on a violation.
    """
    # (a) The transport "error" status and the feature "error" outcome are
    #     paired: one is "error" iff the other is. A handler-level crash sets
    #     BOTH; every non-crash response (including the not_implemented stub)
    #     sets NEITHER. A one-sided "error" means a corrupted payload.
    if (status == "error") != (outcome == "error"):
        raise ShowNumberedOverlayResponseSchemaError(
            "status/outcome 'error' mismatch: status="
            f"{status!r} outcome={outcome!r} (status is 'error' iff outcome is)"
        )

    # (a2) The surviving Phase 1.5 stub is pinned to exactly ONE envelope:
    #      status="not_implemented" is emitted ONLY with
    #      outcome="execution_failed" (mirroring the docstring's pinned stub
    #      envelope). No other outcome pairs with not_implemented, so a
    #      not_implemented payload carrying ok / no_targets / snapshot_expired
    #      could not have come from the stub and must not cross the wh-uf54
    #      trust boundary parsing as trustworthy. The snapshot fields need no
    #      re-assertion here: execution_failed is in _NO_SNAPSHOT_OUTCOMES, so
    #      rule (d) below already forces snapshot_id=None and summary=None.
    #      not_implemented is not "error", so rule (a) above is unaffected.
    if status == "not_implemented" and outcome != "execution_failed":
        raise ShowNumberedOverlayResponseSchemaError(
            "status='not_implemented' requires outcome='execution_failed' "
            f"(the pinned stub envelope), got outcome={outcome!r}"
        )

    # (b) A successful paint (outcome="ok") MUST carry a non-empty snapshot_id
    #     AND a summary -- the snapshot the overlay paints and a later
    #     click_snapshot_item keys on. The presence rule is scoped to "ok"
    #     ONLY: no_targets may carry an empty-items summary OR None, so it is
    #     deliberately NOT constrained here. An empty-string snapshot_id parses
    #     as a str but is not a usable id, so ``not snapshot_id`` rejects both
    #     None and "".
    if outcome == "ok":
        if not snapshot_id:
            raise ShowNumberedOverlayResponseSchemaError(
                "outcome='ok' requires a non-empty snapshot_id, got "
                f"{snapshot_id!r}"
            )
        if snapshot_summary is None:
            raise ShowNumberedOverlayResponseSchemaError(
                "outcome='ok' requires a non-None snapshot_summary"
            )

    # (c) When BOTH the top-level snapshot_id and a summary are present, they
    #     must name the SAME snapshot -- Logic keys the retained summary by the
    #     top-level id, so a disagreement would mis-key "click N". This also
    #     covers no_targets-with-summary (both present) without constraining the
    #     no_targets-with-None case.
    if snapshot_id is not None and snapshot_summary is not None:
        if snapshot_summary.snapshot_id != snapshot_id:
            raise ShowNumberedOverlayResponseSchemaError(
                "snapshot_id disagreement: top-level "
                f"{snapshot_id!r} != snapshot_summary.snapshot_id "
                f"{snapshot_summary.snapshot_id!r}"
            )

    # (d) A FAILURE outcome (snapshot_expired / execution_failed / error)
    #     carries NO snapshot at all -- the handler emits each ONLY with
    #     snapshot_id=None AND summary=None (no walk produced a paintable
    #     snapshot; a stale/gone snapshot has nothing to paint). A
    #     snapshot-bearing failure could not have come from the handler;
    #     accepting it would let Logic cache or correlate a snapshot for a
    #     response that says no valid walk exists. no_targets is deliberately
    #     excluded -- it is a successful walk of an empty window and keeps its
    #     documented snapshot-or-None flexibility.
    if outcome in _NO_SNAPSHOT_OUTCOMES:
        if snapshot_id is not None:
            raise ShowNumberedOverlayResponseSchemaError(
                f"outcome={outcome!r} must carry snapshot_id=None, got "
                f"{snapshot_id!r}"
            )
        if snapshot_summary is not None:
            raise ShowNumberedOverlayResponseSchemaError(
                f"outcome={outcome!r} must carry snapshot_summary=None"
            )

    # (e) no_targets means nothing is paintable, so a summary WITH items
    #     contradicts the outcome (wh-overlay-no-targets-summary-guard). The
    #     handler emits no_targets only when the (possibly filtered) summary
    #     has no items ("never a populated one"), so a populated-items
    #     no_targets could not have come from it. The empty-items-summary-OR-
    #     None flexibility documented above is kept: only the populated-items
    #     combination is rejected. Mirrors start_overlay_walk rule (e).
    if (
        outcome == "no_targets"
        and snapshot_summary is not None
        and snapshot_summary.items
    ):
        raise ShowNumberedOverlayResponseSchemaError(
            "outcome='no_targets' must not carry a populated-items "
            f"snapshot_summary, got {len(snapshot_summary.items)} item(s)"
        )


def _require_str(payload: Mapping[Any, Any], key: str) -> str:
    if key not in payload:
        raise ShowNumberedOverlayResponseSchemaError(
            f"payload missing required field {key!r}"
        )
    value = payload[key]
    if not isinstance(value, str):
        raise ShowNumberedOverlayResponseSchemaError(
            f"field {key!r} must be a str, got {type(value).__name__}"
        )
    return value


def _require_closed_str(
    payload: Mapping[Any, Any], key: str, allowed: frozenset[str]
) -> str:
    value = _require_str(payload, key)
    if value not in allowed:
        raise ShowNumberedOverlayResponseSchemaError(
            f"field {key!r} must be one of {sorted(allowed)}, got {value!r}"
        )
    return value


def _require_optional_str(payload: Mapping[Any, Any], key: str) -> str | None:
    if key not in payload:
        raise ShowNumberedOverlayResponseSchemaError(
            f"payload missing required field {key!r}"
        )
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ShowNumberedOverlayResponseSchemaError(
            f"field {key!r} must be a str or None, got {type(value).__name__}"
        )
    return value


def _require_int(payload: Mapping[Any, Any], key: str) -> int:
    if key not in payload:
        raise ShowNumberedOverlayResponseSchemaError(
            f"payload missing required field {key!r}"
        )
    value = payload[key]
    # bool is a subclass of int; exclude it explicitly so an echoed True is
    # not silently read as 1 (the generation fields are real counts).
    if isinstance(value, bool) or not isinstance(value, int):
        raise ShowNumberedOverlayResponseSchemaError(
            f"field {key!r} must be an int, got {type(value).__name__}"
        )
    return value
