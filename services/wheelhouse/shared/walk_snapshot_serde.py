"""Shared WalkSnapshotSummary (de)serialization for IPC schemas (wh-jfavj).

The voice-element-clicking feature (epic wh-l4h.1) carries a plain-data
``WalkSnapshotSummary`` across the Input -> Logic -> GUI process boundary
in more than one schema: ``click_element.ClickElementResponse`` (wh-med6f)
and ``show_numbered_overlay.ShowNumberedOverlayResponse`` (wh-jfavj). This
module factors the nested-summary serialization out so the two schemas
share one defensive implementation instead of copy-pasting the validation.

The functions raise a caller-supplied ``error_class`` (a subclass of
``ValueError``) on any structural problem, so each schema's ``from_dict``
keeps surfacing its own typed ``*SchemaError`` per wh-uf54: a malformed
payload is logged and dropped at the IPC boundary, never crashed on a
raw ``KeyError`` / ``TypeError`` / ``AttributeError``.

The defensive style matches ``click_element.py`` exactly: not-a-mapping
guards, missing-field guards, EXACT-type (not ``isinstance``) checks on
list/tuple fields to fence hostile subclasses whose dunders raise before
any iteration runs (wh-9f3t.12.1), and a bool-excluded int check
(``bool`` is a subclass of ``int``). ``created_at_monotonic`` must be a
finite number; ``nan`` would silently break the round-trip equality
contract (wh-9f3t.13.2).

NOTE: this module does NOT edit or import ``click_element.py``. It is a
new home for the same logic so a schema that cannot edit
``click_element.py`` (it is owned by wh-med6f) can still reuse it. The
``click_element.py`` copy of ``_summary_to_dict`` / ``_summary_from_dict``
is left untouched by this slice.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Type

from ui.element_types import WalkSnapshotSummary, WalkSnapshotSummaryItem


# Required string fields on a serialized WalkSnapshotSummaryItem.
_ITEM_STRING_FIELDS = ("item_id", "name", "role")


def summary_to_dict(
    summary: WalkSnapshotSummary | None,
) -> dict[str, Any] | None:
    """Serialize a WalkSnapshotSummary to JSON-friendly primitives.

    Returns ``None`` when ``summary`` is ``None``. ``items`` is emitted as
    a list and each item's ``bounds`` as a 4-tuple (round-trips through the
    list normalization in :func:`summary_from_dict`).
    """

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


def summary_from_dict(
    raw: Any,
    error_class: Type[ValueError],
) -> WalkSnapshotSummary | None:
    """Reconstruct a WalkSnapshotSummary from the nested wire dict.

    Returns ``None`` when ``raw`` is ``None``. Raises ``error_class`` (the
    caller's typed schema error, a subclass of ``ValueError``) on any
    structural problem in the nested structure, so the caller's
    ``from_dict`` never leaks a raw ``KeyError`` / ``TypeError`` /
    ``AttributeError`` past the wh-uf54 graceful-degrade boundary.
    """

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise error_class(
            "snapshot_summary must be a mapping or None, "
            f"got {type(raw).__name__}"
        )

    if "snapshot_id" not in raw:
        raise error_class(
            "snapshot_summary missing required field 'snapshot_id'"
        )
    snapshot_id = raw["snapshot_id"]
    if not isinstance(snapshot_id, str):
        raise error_class(
            "snapshot_summary field 'snapshot_id' must be a str, "
            f"got {type(snapshot_id).__name__}"
        )

    if "created_at_monotonic" not in raw:
        raise error_class(
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
        raise error_class(
            "snapshot_summary field 'created_at_monotonic' must be a finite "
            f"number, got {created_at!r}"
        )

    if "items" not in raw:
        raise error_class(
            "snapshot_summary missing required field 'items'"
        )
    raw_items = raw["items"]
    # The v5 contract types items as list[WalkSnapshotSummaryItem] and
    # to_dict emits a list, so items is list-only (a tuple is rejected).
    # The EXACT-type check (not isinstance) also fences out a hostile list
    # subclass whose dunders raise, before any iteration (wh-9f3t.12.1).
    if type(raw_items) is not list:
        raise error_class(
            "snapshot_summary field 'items' must be a builtin list, "
            f"got {type(raw_items).__name__}"
        )

    items = [_summary_item_from_dict(item, error_class) for item in raw_items]
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id,
        items=items,
        created_at_monotonic=float(created_at),
    )


def _summary_item_from_dict(
    raw: Any,
    error_class: Type[ValueError],
) -> WalkSnapshotSummaryItem:
    """Reconstruct a WalkSnapshotSummaryItem from a nested wire dict."""

    if not isinstance(raw, Mapping):
        raise error_class(
            "snapshot_summary 'items' contains a non-mapping member: "
            f"{type(raw).__name__}"
        )

    for name in _ITEM_STRING_FIELDS:
        if name not in raw:
            raise error_class(
                f"snapshot_summary item missing required field {name!r}"
            )
        if not isinstance(raw[name], str):
            raise error_class(
                f"snapshot_summary item field {name!r} must be a str, "
                f"got {type(raw[name]).__name__}"
            )

    for name in ("display_number", "monitor_id"):
        if name not in raw:
            raise error_class(
                f"snapshot_summary item missing required field {name!r}"
            )
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise error_class(
                f"snapshot_summary item field {name!r} must be an int, "
                f"got {type(value).__name__}"
            )

    if "bounds" not in raw:
        raise error_class(
            "snapshot_summary item missing required field 'bounds'"
        )
    bounds = _parse_bounds(raw["bounds"], error_class)

    return WalkSnapshotSummaryItem(
        item_id=raw["item_id"],
        display_number=raw["display_number"],
        name=raw["name"],
        role=raw["role"],
        bounds=bounds,
        monitor_id=raw["monitor_id"],
    )


def _parse_bounds(
    raw: Any,
    error_class: Type[ValueError],
) -> tuple[int, int, int, int]:
    """Validate and normalize a 4-int bounds field to a tuple.

    Accepts the EXACT builtin ``list`` or ``tuple`` type of exactly four
    ints (a JSON-bridged transport delivers the tuple as a list). The
    exact-type check (not ``isinstance``) fences out a hostile list
    subclass whose ``__len__`` / ``__getitem__`` / ``__iter__`` raises,
    before any of those dunders runs (wh-9f3t.12.1). ``bool`` is excluded
    explicitly.
    """

    if type(raw) is not list and type(raw) is not tuple:
        raise error_class(
            "snapshot_summary item field 'bounds' must be a builtin list "
            f"or tuple, got {type(raw).__name__}"
        )
    if len(raw) != 4:
        raise error_class(
            "snapshot_summary item field 'bounds' must have exactly 4 "
            f"elements, got {len(raw)}"
        )
    for member in raw:
        if isinstance(member, bool) or not isinstance(member, int):
            raise error_class(
                "snapshot_summary item field 'bounds' contains a non-int "
                f"member: {type(member).__name__}"
            )
    return (int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]))
