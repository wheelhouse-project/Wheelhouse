"""ClickElementResponse IPC schema (wh-med6f).

Defines the Input -> Logic reply for the ``click_element`` action, Phase 1
of the voice-element-clicking feature (epic wh-l4h.1). The authoritative
field spec lives in the v5 design doc:
``docs/plans/2026-05-21-voice-element-clicking-design-v5.md`` under
"click_element Input -> Logic IPC contract".

Unlike ``text_target_rejected`` (an unsolicited, type-routed event), this
is a request-correlated Schema A response: the Input-process handler emits
exactly one response per ``request_id`` via the ``_HANDLES_OWN_RESPONSE``
machinery, and Logic's awaiter correlates it by ``request_id``. Because the
correlation is handled by the request_id envelope rather than a payload
``type`` key, this schema carries no routing ``type`` field; ``status`` is
the Schema A status field.

Transport: the Input Process puts a dict produced by
``ClickElementResponse.to_dict()`` onto the response queue. The Logic
Process should call ``ClickElementResponse.from_dict()`` and catch
``ClickElementResponseSchemaError`` for graceful degradation (log + drop)
on a malformed payload, per wh-uf54.

Field meanings:
  * ``status`` -- Schema A status, ``"ok"`` or ``"error"``.
  * ``outcome`` -- ``"ok"``, ``"not_found"``, ``"ambiguous"``, or
    ``"execution_failed"``.
  * ``reason`` -- machine-readable tag (``str``) set when
    ``outcome == "execution_failed"`` (e.g. ``"disabled"``,
    ``"bounds_invalid"``, ``"foreground_changed"``,
    ``"foreground_verification_failed"``, ``"invoke_com_error"``,
    ``"invoke_then_sendinput_failed"``, ``"sendinput_short"``,
    ``"target_moved_offscreen"``, ``"timeout"``); ``None`` otherwise.
    The concrete tags are not enumerated in the schema so the executor
    can adjust the set without touching the contract.
  * ``matched_names`` -- tuple of matched control names (up to
    ``notice_max_names`` entries for the ``ambiguous`` outcome). Empty
    tuple is valid. Normalized from a list on a JSON-bridged transport.
  * ``snapshot_id`` -- the walk snapshot id so Logic can chain the
    Phase 1.5 numbered overlay; ``None`` when no walk produced one.
  * ``snapshot_summary`` -- the plain-data ``WalkSnapshotSummary`` the GUI
    paints for the numbered overlay, or ``None``. Serialized to / from
    JSON-friendly primitives by this schema (the type itself has no
    to_dict / from_dict).
  * ``matched_name`` -- the single matched name used for
    ``execution_failed`` notice wording; ``None`` otherwise.
  * ``trace_id`` -- the Logic-generated trace id (wh-l4h.1.6.10) so
    Input-emitted log lines and the Logic awaiter share one correlation
    id. The generate-and-propagate wiring is a downstream slice
    (wh-tab7j); this schema only carries and round-trips the id.
  * ``ambiguous_item_ids`` -- optional tuple of element ids for the
    ``ambiguous`` outcome (the snapshot items the user must disambiguate
    between); ``None`` for every non-ambiguous outcome. Optional with a
    ``None`` default: an absent wire key parses to ``None`` (backward
    compatible) and ``to_dict`` OMITS the key when ``None`` so the
    non-ambiguous response carries nothing for it on the wire. The
    producer that sets it is a downstream slice (wh-ynr5zb); this slice is
    schema-only. Normalized from a list on a JSON-bridged transport.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.schema_guard import reraise_as_schema_error

from ui.element_types import WalkSnapshotSummary, WalkSnapshotSummaryItem


class ClickElementResponseSchemaError(ValueError):
    """Raised by ``ClickElementResponse.from_dict`` on a malformed payload.

    The Logic process should catch this and degrade gracefully
    (log + drop), per wh-uf54. ``from_dict`` never lets a raw
    ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.
    """


# Required fields whose declared type is ``str | None``: present, but
# either a string or None.
_OPTIONAL_STRING_FIELDS = ("reason", "snapshot_id", "matched_name")

# Required fields whose declared type is ``str``.
_STRING_FIELDS = ("status", "outcome", "trace_id")

# Closed-set membership for the two Literal fields in the v5 contract
# ("click_element Input -> Logic IPC contract"):
#   status:  Literal["ok", "error"]
#   outcome: Literal["ok", "not_found", "ambiguous", "execution_failed"]
# ``reason`` is deliberately NOT constrained -- it is an open executor
# tag set, matching the sibling text_target_rejection.py which leaves its
# own ``reason`` unconstrained.
_ALLOWED_STATUS = frozenset({"ok", "error"})
_ALLOWED_OUTCOME = frozenset(
    {"ok", "not_found", "ambiguous", "execution_failed"}
)

# Required field set for a serialized WalkSnapshotSummaryItem.
_ITEM_STRING_FIELDS = ("item_id", "name", "role")


@dataclass(frozen=True)
class ClickElementResponse:
    """Structured Input -> Logic reply for the click_element action."""

    status: str
    outcome: str
    reason: str | None
    matched_names: tuple[str, ...]
    snapshot_id: str | None
    snapshot_summary: WalkSnapshotSummary | None
    matched_name: str | None
    trace_id: str
    ambiguous_item_ids: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        ``matched_names`` is emitted as a tuple (round-trips through the
        list normalization in ``from_dict``). ``snapshot_summary`` is
        serialized to a nested dict of JSON-friendly primitives, or
        ``None`` when absent. ``ambiguous_item_ids`` is OMITTED entirely
        when ``None`` (the non-ambiguous response carries nothing for it
        on the wire) and emitted as a tuple otherwise (round-trips through
        the list normalization in ``from_dict``).
        """

        payload: dict[str, Any] = {
            "status": self.status,
            "outcome": self.outcome,
            "reason": self.reason,
            "matched_names": self.matched_names,
            "snapshot_id": self.snapshot_id,
            "snapshot_summary": _summary_to_dict(self.snapshot_summary),
            "matched_name": self.matched_name,
            "trace_id": self.trace_id,
        }
        if self.ambiguous_item_ids is not None:
            payload["ambiguous_item_ids"] = self.ambiguous_item_ids
        return payload

    @classmethod
    @reraise_as_schema_error(ClickElementResponseSchemaError)
    def from_dict(cls, payload: Any) -> "ClickElementResponse":
        """Parse and validate a wire-format dict.

        Raises ``ClickElementResponseSchemaError`` on any structural
        problem: not a mapping, missing required field, wrong field type,
        a ``status`` / ``outcome`` value outside its closed set,
        non-string member of ``matched_names``, or a malformed nested
        ``snapshot_summary``. ``matched_names`` is normalized to a tuple
        and nested item ``bounds`` are normalized to a 4-int tuple, so a
        sender that uses lists (e.g. via a JSON-bridged transport) is
        accepted.

        ``ambiguous_item_ids`` is OPTIONAL: an absent key (or an explicit
        ``None``) yields ``None`` (backward compatible -- a producer that
        predates the field is accepted), while a present list/tuple of
        str is validated and normalized to a tuple. A present non-list/
        tuple value, or a non-string member, raises the schema error.
        """

        if not isinstance(payload, Mapping):
            raise ClickElementResponseSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        for name in _STRING_FIELDS:
            if name not in payload:
                raise ClickElementResponseSchemaError(
                    f"payload missing required field {name!r}"
                )
            value = payload[name]
            if not isinstance(value, str):
                raise ClickElementResponseSchemaError(
                    f"field {name!r} must be a str, got {type(value).__name__}"
                )

        # Closed-set membership for the two Literal fields (v5 contract).
        if payload["status"] not in _ALLOWED_STATUS:
            raise ClickElementResponseSchemaError(
                f"field 'status' must be one of {sorted(_ALLOWED_STATUS)}, "
                f"got {payload['status']!r}"
            )
        if payload["outcome"] not in _ALLOWED_OUTCOME:
            raise ClickElementResponseSchemaError(
                f"field 'outcome' must be one of {sorted(_ALLOWED_OUTCOME)}, "
                f"got {payload['outcome']!r}"
            )

        for name in _OPTIONAL_STRING_FIELDS:
            if name not in payload:
                raise ClickElementResponseSchemaError(
                    f"payload missing required field {name!r}"
                )
            value = payload[name]
            if value is not None and not isinstance(value, str):
                raise ClickElementResponseSchemaError(
                    f"field {name!r} must be a str or None, "
                    f"got {type(value).__name__}"
                )

        if "matched_names" not in payload:
            raise ClickElementResponseSchemaError(
                "payload missing required field 'matched_names'"
            )
        matched_names = _parse_matched_names(payload["matched_names"])

        if "snapshot_summary" not in payload:
            raise ClickElementResponseSchemaError(
                "payload missing required field 'snapshot_summary'"
            )
        snapshot_summary = _summary_from_dict(payload["snapshot_summary"])

        return cls(
            status=payload["status"],
            outcome=payload["outcome"],
            reason=payload["reason"],
            matched_names=matched_names,
            snapshot_id=payload["snapshot_id"],
            snapshot_summary=snapshot_summary,
            matched_name=payload["matched_name"],
            trace_id=payload["trace_id"],
            # Read the optional field with the same ``in`` + ``[]`` idiom
            # the 8 required fields above use, NOT ``payload.get(...)``.
            # ``from_dict`` accepts any Mapping (isinstance gate), so a
            # hostile Mapping subclass with a normal ``__getitem__`` but a
            # ``get`` that raises would let a non-schema exception escape
            # the wh-uf54 graceful-degrade boundary at this single call
            # site (wh-n29v.109.1). Using ``in``/``[]`` keeps this field no
            # more exposed than the others; an absent key or explicit None
            # both yield None as before.
            ambiguous_item_ids=_parse_ambiguous_item_ids(
                payload["ambiguous_item_ids"]
                if "ambiguous_item_ids" in payload
                else None
            ),
        )


def _parse_matched_names(raw: Any) -> tuple[str, ...]:
    """Validate and normalize ``matched_names`` to a tuple of str.

    Restricts the accepted shapes to the EXACT builtin ``list`` or
    ``tuple`` type so iteration is safe. An ``isinstance`` check would
    pass a hostile ``list`` subclass whose ``__iter__`` / ``__len__`` /
    ``__getitem__`` raises, leaking a non-schema exception past the
    wh-uf54 graceful-degrade boundary (wh-9f3t.12.1); the exact-type gate
    rejects the subclass before any such dunder runs. The documented wire
    shapes -- a tuple from ``to_dict`` and a list from a JSON-bridged
    transport -- both pass.
    """

    if type(raw) is not list and type(raw) is not tuple:
        raise ClickElementResponseSchemaError(
            "field 'matched_names' must be a builtin list or tuple of str, "
            f"got {type(raw).__name__}"
        )
    names: list[str] = []
    for member in raw:
        if not isinstance(member, str):
            raise ClickElementResponseSchemaError(
                "field 'matched_names' contains non-string member: "
                f"{type(member).__name__}"
            )
        names.append(member)
    return tuple(names)


def _parse_ambiguous_item_ids(raw: Any) -> tuple[str, ...] | None:
    """Validate and normalize the optional ``ambiguous_item_ids`` field.

    Mirrors ``_parse_matched_names`` (the EXACT builtin ``list`` / ``tuple``
    type gate that rejects a hostile subclass before any dunder runs, then a
    per-member ``str`` check, returning a tuple), with a leading ``None``
    guard: an absent wire key arrives here as ``None`` (``from_dict`` reads it
    with the same ``in`` + ``[]`` membership idiom the required fields use,
    passing ``None`` when the key is absent -- NOT ``payload.get``; see the
    call-site comment for why) and yields ``None`` so the non-ambiguous
    response and a producer that predates the field both round-trip. A present
    non-list/tuple value, or a non-string member, raises the schema error.
    """

    if raw is None:
        return None
    if type(raw) is not list and type(raw) is not tuple:
        raise ClickElementResponseSchemaError(
            "field 'ambiguous_item_ids' must be a builtin list or tuple of "
            f"str, got {type(raw).__name__}"
        )
    ids: list[str] = []
    for member in raw:
        if not isinstance(member, str):
            raise ClickElementResponseSchemaError(
                "field 'ambiguous_item_ids' contains non-string member: "
                f"{type(member).__name__}"
            )
        ids.append(member)
    return tuple(ids)


def _summary_to_dict(
    summary: WalkSnapshotSummary | None,
) -> dict[str, Any] | None:
    """Serialize a WalkSnapshotSummary to JSON-friendly primitives."""

    if summary is None:
        return None
    return {
        "snapshot_id": summary.snapshot_id,
        "created_at_monotonic": summary.created_at_monotonic,
        "items": [
            {
                "item_id": item.item_id,
                "display_number": item.display_number,
                "name": item.name,
                "role": item.role,
                "bounds": tuple(item.bounds),
                "monitor_id": item.monitor_id,
            }
            for item in summary.items
        ],
    }


def _summary_from_dict(raw: Any) -> WalkSnapshotSummary | None:
    """Reconstruct a WalkSnapshotSummary from the nested wire dict.

    Returns ``None`` when ``raw`` is ``None``. Raises
    ``ClickElementResponseSchemaError`` on any structural problem in the
    nested structure.
    """

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ClickElementResponseSchemaError(
            "field 'snapshot_summary' must be a mapping or None, "
            f"got {type(raw).__name__}"
        )

    if "snapshot_id" not in raw:
        raise ClickElementResponseSchemaError(
            "snapshot_summary missing required field 'snapshot_id'"
        )
    snapshot_id = raw["snapshot_id"]
    if not isinstance(snapshot_id, str):
        raise ClickElementResponseSchemaError(
            "snapshot_summary field 'snapshot_id' must be a str, "
            f"got {type(snapshot_id).__name__}"
        )

    if "created_at_monotonic" not in raw:
        raise ClickElementResponseSchemaError(
            "snapshot_summary missing required field 'created_at_monotonic'"
        )
    created_at = raw["created_at_monotonic"]
    # bool is a subclass of int; exclude it explicitly. A float or int is
    # accepted (JSON numbers may arrive as either). A non-finite float
    # (nan / inf) is rejected (wh-9f3t.13.2): nan != nan would silently
    # break the to_dict/from_dict round-trip equality contract.
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or (isinstance(created_at, float) and not math.isfinite(created_at))
    ):
        raise ClickElementResponseSchemaError(
            "snapshot_summary field 'created_at_monotonic' must be a finite "
            f"number, got {created_at!r}"
        )

    if "items" not in raw:
        raise ClickElementResponseSchemaError(
            "snapshot_summary missing required field 'items'"
        )
    raw_items = raw["items"]
    # wh-9f3t.12.2: the v5 contract types items as
    # list[WalkSnapshotSummaryItem] and to_dict emits a list, so items is
    # list-only (a tuple is rejected). The EXACT-type check (not
    # isinstance) also fences out a hostile list subclass whose dunders
    # raise, before any iteration (wh-9f3t.12.1).
    if type(raw_items) is not list:
        raise ClickElementResponseSchemaError(
            "snapshot_summary field 'items' must be a builtin list, "
            f"got {type(raw_items).__name__}"
        )

    items = [_summary_item_from_dict(item) for item in raw_items]
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id,
        items=items,
        created_at_monotonic=float(created_at),
    )


def _summary_item_from_dict(raw: Any) -> WalkSnapshotSummaryItem:
    """Reconstruct a WalkSnapshotSummaryItem from a nested wire dict."""

    if not isinstance(raw, Mapping):
        raise ClickElementResponseSchemaError(
            "snapshot_summary 'items' contains a non-mapping member: "
            f"{type(raw).__name__}"
        )

    for name in _ITEM_STRING_FIELDS:
        if name not in raw:
            raise ClickElementResponseSchemaError(
                f"snapshot_summary item missing required field {name!r}"
            )
        if not isinstance(raw[name], str):
            raise ClickElementResponseSchemaError(
                f"snapshot_summary item field {name!r} must be a str, "
                f"got {type(raw[name]).__name__}"
            )

    for name in ("display_number", "monitor_id"):
        if name not in raw:
            raise ClickElementResponseSchemaError(
                f"snapshot_summary item missing required field {name!r}"
            )
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ClickElementResponseSchemaError(
                f"snapshot_summary item field {name!r} must be an int, "
                f"got {type(value).__name__}"
            )

    if "bounds" not in raw:
        raise ClickElementResponseSchemaError(
            "snapshot_summary item missing required field 'bounds'"
        )
    bounds = _parse_bounds(raw["bounds"])

    return WalkSnapshotSummaryItem(
        item_id=raw["item_id"],
        display_number=raw["display_number"],
        name=raw["name"],
        role=raw["role"],
        bounds=bounds,
        monitor_id=raw["monitor_id"],
    )


def _parse_bounds(raw: Any) -> tuple[int, int, int, int]:
    """Validate and normalize a 4-int bounds field to a tuple.

    Accepts the EXACT builtin ``list`` or ``tuple`` type of exactly four
    ints (a JSON-bridged transport delivers the tuple as a list). The
    exact-type check (not ``isinstance``) fences out a hostile list
    subclass whose ``__len__`` / ``__getitem__`` / ``__iter__`` raises,
    before any of those dunders runs (wh-9f3t.12.1). ``bool`` is excluded
    explicitly.
    """

    if type(raw) is not list and type(raw) is not tuple:
        raise ClickElementResponseSchemaError(
            "snapshot_summary item field 'bounds' must be a builtin list "
            f"or tuple, got {type(raw).__name__}"
        )
    if len(raw) != 4:
        raise ClickElementResponseSchemaError(
            "snapshot_summary item field 'bounds' must have exactly 4 "
            f"elements, got {len(raw)}"
        )
    for member in raw:
        if isinstance(member, bool) or not isinstance(member, int):
            raise ClickElementResponseSchemaError(
                "snapshot_summary item field 'bounds' contains a non-int "
                f"member: {type(member).__name__}"
            )
    return (int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]))
