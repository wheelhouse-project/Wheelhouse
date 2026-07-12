"""Tests for the ClickElementResponse IPC schema (wh-med6f).

The schema defines the Input -> Logic reply for the click_element action
(Phase 1 of the voice-element-clicking feature, epic wh-l4h.1). It is a
request-correlated Schema A response (handler-owned, correlated by
request_id via the _HANDLES_OWN_RESPONSE machinery), not a type-routed
unsolicited event like text_target_rejected.

Coverage:
  * Full round-trip (to_dict -> from_dict) for an "ok" outcome with a
    populated multi-item WalkSnapshotSummary.
  * Round-trip for "not_found" with snapshot_summary=None and empty
    matched_names.
  * Round-trip for "ambiguous" with multiple matched_names.
  * Round-trip for "execution_failed" with a reason tag and a matched_name.
  * matched_names normalization: a list on the wire becomes a tuple.
  * Nested snapshot_summary item bounds normalization: a list becomes a tuple.
  * trace_id is carried and round-trips.
  * from_dict raises ClickElementResponseSchemaError on every malformed
    shape (not a mapping, missing required field, wrong field type,
    non-string matched_names member, malformed nested snapshot_summary) --
    never an unhandled KeyError / TypeError / AttributeError.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.click_element import (
    ClickElementResponse,
    ClickElementResponseSchemaError,
)
from ui.element_types import WalkSnapshotSummary, WalkSnapshotSummaryItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summary(num_items: int = 2) -> WalkSnapshotSummary:
    items = [
        WalkSnapshotSummaryItem(
            item_id=f"m{i}",
            display_number=i,
            name=f"Cancel {i}",
            role="Button",
            bounds=(10 * i, 20 * i, 80 + i, 30 + i),
            monitor_id=i % 2,
        )
        for i in range(1, num_items + 1)
    ]
    return WalkSnapshotSummary(
        snapshot_id="s1",
        items=items,
        created_at_monotonic=123.456,
    )


def _ok_response(**overrides) -> ClickElementResponse:
    fields = dict(
        status="ok",
        outcome="ok",
        reason=None,
        matched_names=("Cancel",),
        snapshot_id="s1",
        snapshot_summary=_summary(2),
        matched_name="Cancel",
        trace_id="trace-abc-123",
    )
    fields.update(overrides)
    return ClickElementResponse(**fields)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_ok_with_populated_summary():
    original = _ok_response()
    restored = ClickElementResponse.from_dict(original.to_dict())
    assert restored == original
    assert restored.snapshot_summary is not None
    assert len(restored.snapshot_summary.items) == 2
    assert all(
        isinstance(i, WalkSnapshotSummaryItem)
        for i in restored.snapshot_summary.items
    )
    assert isinstance(restored.snapshot_summary, WalkSnapshotSummary)


def test_round_trip_not_found_none_summary_empty_names():
    original = ClickElementResponse(
        status="ok",
        outcome="not_found",
        reason=None,
        matched_names=(),
        snapshot_id=None,
        snapshot_summary=None,
        matched_name=None,
        trace_id="trace-nf",
    )
    restored = ClickElementResponse.from_dict(original.to_dict())
    assert restored == original
    assert restored.snapshot_summary is None
    assert restored.matched_names == ()


def test_round_trip_ambiguous_with_ambiguous_item_ids():
    original = ClickElementResponse(
        status="ok",
        outcome="ambiguous",
        reason=None,
        matched_names=("Cancel", "Cancel all"),
        snapshot_id="s7",
        snapshot_summary=_summary(2),
        matched_name=None,
        trace_id="trace-amb-ids",
        ambiguous_item_ids=("i1", "i2"),
    )
    restored = ClickElementResponse.from_dict(original.to_dict())
    assert restored == original
    assert restored.ambiguous_item_ids == ("i1", "i2")


def test_round_trip_ambiguous_multiple_names():
    original = ClickElementResponse(
        status="ok",
        outcome="ambiguous",
        reason=None,
        matched_names=("Cancel", "Cancel and exit", "Cancel all"),
        snapshot_id="s2",
        snapshot_summary=_summary(3),
        matched_name=None,
        trace_id="trace-amb",
    )
    restored = ClickElementResponse.from_dict(original.to_dict())
    assert restored == original
    assert restored.matched_names == ("Cancel", "Cancel and exit", "Cancel all")


def test_round_trip_execution_failed_with_reason_and_matched_name():
    original = ClickElementResponse(
        status="ok",
        outcome="execution_failed",
        reason="disabled",
        matched_names=("Submit",),
        snapshot_id="s3",
        snapshot_summary=None,
        matched_name="Submit",
        trace_id="trace-ef",
    )
    restored = ClickElementResponse.from_dict(original.to_dict())
    assert restored == original
    assert restored.reason == "disabled"
    assert restored.matched_name == "Submit"


def test_status_error_round_trips():
    original = _ok_response(status="error", outcome="execution_failed", reason="timeout")
    restored = ClickElementResponse.from_dict(original.to_dict())
    assert restored == original
    assert restored.status == "error"


# ---------------------------------------------------------------------------
# Closed-set validation for status and outcome (wh-9f3t.11.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["ok", "error"])
def test_from_dict_accepts_valid_status(status: str):
    payload = _ok_response(status=status).to_dict()
    restored = ClickElementResponse.from_dict(payload)
    assert restored.status == status


@pytest.mark.parametrize(
    "outcome", ["ok", "not_found", "ambiguous", "execution_failed"]
)
def test_from_dict_accepts_valid_outcome(outcome: str):
    payload = _ok_response(outcome=outcome).to_dict()
    restored = ClickElementResponse.from_dict(payload)
    assert restored.outcome == outcome


def test_from_dict_rejects_unrecognized_status():
    payload = _ok_response().to_dict()
    payload["status"] = "garbage"
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "status" in str(exc_info.value)


def test_from_dict_rejects_unrecognized_outcome():
    payload = _ok_response().to_dict()
    payload["outcome"] = "nonsense"
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "outcome" in str(exc_info.value)


def test_from_dict_leaves_reason_an_open_tag_set():
    """reason is an open str-or-None tag (mirrors text_target_rejection.py,
    which leaves its reason unconstrained). An arbitrary string must pass."""
    payload = _ok_response(
        outcome="execution_failed", reason="some_brand_new_executor_tag"
    ).to_dict()
    restored = ClickElementResponse.from_dict(payload)
    assert restored.reason == "some_brand_new_executor_tag"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_matched_names_list_normalizes_to_tuple():
    payload = _ok_response(matched_names=("a", "b")).to_dict()
    payload["matched_names"] = ["a", "b"]  # JSON-bridged transport delivers a list
    restored = ClickElementResponse.from_dict(payload)
    assert restored.matched_names == ("a", "b")
    assert isinstance(restored.matched_names, tuple)


def test_summary_item_bounds_list_normalizes_to_tuple():
    payload = _ok_response().to_dict()
    # Simulate a JSON-bridged transport delivering bounds as a list.
    payload["snapshot_summary"]["items"][0]["bounds"] = [1, 2, 3, 4]
    restored = ClickElementResponse.from_dict(payload)
    assert restored.snapshot_summary is not None
    first = restored.snapshot_summary.items[0]
    assert first.bounds == (1, 2, 3, 4)
    assert isinstance(first.bounds, tuple)


def test_trace_id_round_trips():
    original = _ok_response(trace_id="unique-trace-id-xyz")
    payload = original.to_dict()
    assert payload["trace_id"] == "unique-trace-id-xyz"
    restored = ClickElementResponse.from_dict(payload)
    assert restored.trace_id == "unique-trace-id-xyz"


def test_to_dict_serializes_summary_to_primitives():
    payload = _ok_response().to_dict()
    summary = payload["snapshot_summary"]
    assert isinstance(summary, dict)
    assert summary["snapshot_id"] == "s1"
    assert summary["created_at_monotonic"] == pytest.approx(123.456)
    assert isinstance(summary["items"], list)
    first = summary["items"][0]
    assert isinstance(first, dict)
    assert first["item_id"] == "m1"
    assert first["display_number"] == 1
    assert first["name"] == "Cancel 1"
    assert first["role"] == "Button"
    assert first["monitor_id"] == 1


def test_event_is_immutable():
    response = _ok_response()
    with pytest.raises(Exception):
        response.status = "error"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validation: top-level structure
# ---------------------------------------------------------------------------


def test_from_dict_rejects_non_mapping():
    with pytest.raises(ClickElementResponseSchemaError):
        ClickElementResponse.from_dict("not a dict")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "missing_field",
    [
        "status",
        "outcome",
        "reason",
        "matched_names",
        "snapshot_id",
        "snapshot_summary",
        "matched_name",
        "trace_id",
    ],
)
def test_from_dict_rejects_missing_field(missing_field: str):
    payload = _ok_response().to_dict()
    del payload[missing_field]
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert missing_field in str(exc_info.value)


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("status", 42),
        ("outcome", None),
        ("trace_id", 1.5),
    ],
)
def test_from_dict_rejects_non_string_required_field(field: str, bad_value):
    payload = _ok_response().to_dict()
    payload[field] = bad_value
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert field in str(exc_info.value)


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("reason", 42),
        ("snapshot_id", []),
        ("matched_name", 3.14),
    ],
)
def test_from_dict_rejects_wrong_type_optional_string(field: str, bad_value):
    """reason / snapshot_id / matched_name are str | None: a non-str,
    non-None value is rejected."""
    payload = _ok_response().to_dict()
    payload[field] = bad_value
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert field in str(exc_info.value)


def test_from_dict_accepts_none_for_optional_strings():
    payload = _ok_response(
        reason=None, snapshot_id=None, snapshot_summary=None, matched_name=None
    ).to_dict()
    restored = ClickElementResponse.from_dict(payload)
    assert restored.reason is None
    assert restored.snapshot_id is None
    assert restored.matched_name is None
    assert restored.snapshot_summary is None


# ---------------------------------------------------------------------------
# Validation: matched_names
# ---------------------------------------------------------------------------


def test_from_dict_rejects_non_list_tuple_matched_names():
    payload = _ok_response().to_dict()
    payload["matched_names"] = 42
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "matched_names" in str(exc_info.value)


def test_from_dict_rejects_non_string_matched_names_member():
    payload = _ok_response().to_dict()
    payload["matched_names"] = ["Cancel", 42]
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "matched_names" in str(exc_info.value)


def test_from_dict_rejects_arbitrary_iterable_at_isinstance_gate():
    """_parse_matched_names restricts the accepted shapes to list / tuple
    at the isinstance(raw, (list, tuple)) gate, BEFORE any __iter__ is
    called. This verifies that an arbitrary iterable that is neither a
    list nor a tuple is rejected at that gate with the schema error: the
    object's __iter__ never runs (the assertion below proves it), so the
    iteration path is not what does the rejecting -- the type gate is.
    """

    iter_called = {"value": False}

    class ArbitraryIterable:
        def __iter__(self):
            iter_called["value"] = True
            return iter(("Cancel",))

    payload = _ok_response().to_dict()
    payload["matched_names"] = ArbitraryIterable()
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert isinstance(exc_info.value, ClickElementResponseSchemaError)
    assert "matched_names" in str(exc_info.value)
    # The isinstance gate rejected it before iteration was attempted.
    assert iter_called["value"] is False


def test_from_dict_rejects_set_matched_names():
    """A set is iterable but unordered; not in the documented wire shape."""
    payload = _ok_response().to_dict()
    payload["matched_names"] = {"Cancel", "Submit"}
    with pytest.raises(ClickElementResponseSchemaError):
        ClickElementResponse.from_dict(payload)


def test_from_dict_rejects_hostile_list_subclass_matched_names():
    """wh-9f3t.12.1: a list subclass whose __iter__ raises RuntimeError
    would pass an isinstance(raw, (list, tuple)) gate and then leak a
    non-schema exception out of from_dict, violating the wh-uf54
    graceful-degrade contract. The exact-type gate (type(raw) is list/
    tuple) must reject the subclass as a schema error before __iter__
    is ever called -- so RuntimeError never escapes."""

    class HostileList(list):
        def __iter__(self):
            raise RuntimeError("hostile __iter__")

    payload = _ok_response().to_dict()
    payload["matched_names"] = HostileList(["Cancel"])
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert isinstance(exc_info.value, ClickElementResponseSchemaError)
    assert "matched_names" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Validation: ambiguous_item_ids (optional field, wh-ynr5zb schema slice)
# ---------------------------------------------------------------------------


def test_to_dict_omits_ambiguous_item_ids_when_none():
    """The default-None field carries nothing on the wire: to_dict must
    OMIT the key entirely, not emit it as None."""
    payload = _ok_response().to_dict()
    assert "ambiguous_item_ids" not in payload


def test_from_dict_absent_ambiguous_item_ids_parses_to_none():
    """Backward compatibility: a payload with the key entirely absent (a
    producer that predates the field) parses to ambiguous_item_ids=None,
    not a missing-required-field error."""
    payload = _ok_response().to_dict()
    assert "ambiguous_item_ids" not in payload
    restored = ClickElementResponse.from_dict(payload)
    assert restored.ambiguous_item_ids is None


def test_from_dict_explicit_none_ambiguous_item_ids_parses_to_none():
    payload = _ok_response().to_dict()
    payload["ambiguous_item_ids"] = None
    restored = ClickElementResponse.from_dict(payload)
    assert restored.ambiguous_item_ids is None


def test_from_dict_ambiguous_item_ids_list_normalizes_to_tuple():
    """A JSON-bridged transport delivers the field as a list; from_dict
    normalizes it to a tuple."""
    payload = _ok_response().to_dict()
    payload["ambiguous_item_ids"] = ["i1", "i2"]
    restored = ClickElementResponse.from_dict(payload)
    assert restored.ambiguous_item_ids == ("i1", "i2")
    assert isinstance(restored.ambiguous_item_ids, tuple)


def test_from_dict_rejects_non_list_tuple_ambiguous_item_ids():
    payload = _ok_response().to_dict()
    payload["ambiguous_item_ids"] = "i1"
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "ambiguous_item_ids" in str(exc_info.value)


def test_from_dict_rejects_non_string_ambiguous_item_ids_member():
    payload = _ok_response().to_dict()
    payload["ambiguous_item_ids"] = ["i1", 42]
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "ambiguous_item_ids" in str(exc_info.value)


def test_from_dict_rejects_hostile_list_subclass_ambiguous_item_ids():
    """wh-9f3t.12.1: a list subclass whose __iter__ raises RuntimeError
    would pass an isinstance(raw, (list, tuple)) gate and then leak a
    non-schema exception out of from_dict, violating the wh-uf54
    graceful-degrade contract. The exact-type gate (type(raw) is list/
    tuple) in _parse_ambiguous_item_ids must reject the subclass as a
    schema error before __iter__ is ever called -- so RuntimeError never
    escapes. Locks the exact-type gate against a future switch to
    isinstance, matching the matched_names/items/bounds siblings
    (wh-n29v.108.1)."""

    class HostileList(list):
        def __iter__(self):
            raise RuntimeError("hostile __iter__")

    payload = _ok_response().to_dict()
    payload["ambiguous_item_ids"] = HostileList(["i1"])
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert isinstance(exc_info.value, ClickElementResponseSchemaError)
    assert "ambiguous_item_ids" in str(exc_info.value)


def test_from_dict_hostile_get_mapping_subclass_does_not_escape():
    """wh-n29v.109.1: from_dict accepts any Mapping (isinstance gate), and
    the optional ambiguous_item_ids field must be read with the same
    ``in`` + ``[]`` idiom the required fields use, NOT payload.get(...).
    A Mapping subclass whose get() raises but whose __getitem__ /
    __contains__ are normal would otherwise leak a non-schema RuntimeError
    past the wh-uf54 graceful-degrade boundary at that single call site.
    With the in/[] idiom the hostile get() is never called, so an
    otherwise-valid payload parses normally (the key is absent here, so
    ambiguous_item_ids is None). Locks the fix against a revert to
    payload.get(...)."""

    class HostileGetDict(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("hostile get")

    hostile = HostileGetDict(_ok_response().to_dict())
    assert "ambiguous_item_ids" not in hostile
    restored = ClickElementResponse.from_dict(hostile)
    assert restored.ambiguous_item_ids is None
    assert restored == _ok_response()


# ---------------------------------------------------------------------------
# Validation: nested snapshot_summary
# ---------------------------------------------------------------------------


def test_from_dict_rejects_non_mapping_snapshot_summary():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = "not a mapping"
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "snapshot_summary" in str(exc_info.value)


def test_from_dict_rejects_summary_missing_field():
    payload = _ok_response().to_dict()
    del payload["snapshot_summary"]["snapshot_id"]
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "snapshot_id" in str(exc_info.value)


def test_from_dict_rejects_summary_wrong_field_type():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["created_at_monotonic"] = "not a float"
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "created_at_monotonic" in str(exc_info.value)


def test_from_dict_rejects_summary_non_list_items():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["items"] = "not a list"
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "items" in str(exc_info.value)


def test_from_dict_rejects_summary_item_not_mapping():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["items"][0] = "not a mapping"
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "items" in str(exc_info.value)


def test_from_dict_rejects_summary_item_missing_field():
    payload = _ok_response().to_dict()
    del payload["snapshot_summary"]["items"][0]["name"]
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "name" in str(exc_info.value)


def test_from_dict_rejects_summary_item_wrong_field_type():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["items"][0]["display_number"] = "not an int"
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "display_number" in str(exc_info.value)


def test_from_dict_rejects_summary_item_malformed_bounds():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["items"][0]["bounds"] = [1, 2, 3]  # wrong arity
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "bounds" in str(exc_info.value)


def test_from_dict_rejects_summary_item_non_int_bounds_member():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["items"][0]["bounds"] = [1, 2, 3, "x"]
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "bounds" in str(exc_info.value)


def test_from_dict_accepts_empty_summary_items():
    payload = _ok_response(
        snapshot_summary=WalkSnapshotSummary(
            snapshot_id="s9", items=[], created_at_monotonic=0.0
        )
    ).to_dict()
    restored = ClickElementResponse.from_dict(payload)
    assert restored.snapshot_summary is not None
    assert restored.snapshot_summary.items == []


def test_from_dict_rejects_summary_items_as_tuple():
    """wh-9f3t.12.2: the v5 contract types items as
    list[WalkSnapshotSummaryItem] and to_dict emits a list. A tuple is
    not the contract shape for items (unlike matched_names / bounds which
    accept list-or-tuple for JSON-bridge normalization). from_dict must
    reject a tuple here with the schema error."""
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["items"] = tuple(
        payload["snapshot_summary"]["items"]
    )
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "items" in str(exc_info.value)


def test_from_dict_rejects_hostile_list_subclass_items():
    """wh-9f3t.12.1: a list subclass whose __iter__ raises RuntimeError,
    supplied as snapshot_summary.items, must be rejected as a schema error
    at the exact-type gate before __iter__ runs -- RuntimeError must not
    escape from_dict."""

    class HostileList(list):
        def __iter__(self):
            raise RuntimeError("hostile __iter__")

    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["items"] = HostileList(
        payload["snapshot_summary"]["items"]
    )
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert isinstance(exc_info.value, ClickElementResponseSchemaError)
    assert "items" in str(exc_info.value)


def test_from_dict_rejects_hostile_list_subclass_bounds():
    """wh-9f3t.12.1: a list subclass whose __iter__ raises RuntimeError,
    supplied as an item's bounds, must be rejected as a schema error at
    the exact-type gate before any iteration / len / index -- RuntimeError
    must not escape from_dict."""

    class HostileList(list):
        def __iter__(self):
            raise RuntimeError("hostile __iter__")

        def __len__(self):
            raise RuntimeError("hostile __len__")

    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["items"][0]["bounds"] = HostileList([1, 2, 3, 4])
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert isinstance(exc_info.value, ClickElementResponseSchemaError)
    assert "bounds" in str(exc_info.value)


# ---------------------------------------------------------------------------
# created_at_monotonic finiteness (wh-9f3t.13.2)
# ---------------------------------------------------------------------------


def test_from_dict_rejects_created_at_monotonic_nan():
    """nan != nan, so a nan created_at_monotonic silently breaks the
    to_dict/from_dict round-trip equality contract. It must be rejected."""
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["created_at_monotonic"] = float("nan")
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "created_at_monotonic" in str(exc_info.value)


def test_from_dict_rejects_created_at_monotonic_inf():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["created_at_monotonic"] = float("inf")
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "created_at_monotonic" in str(exc_info.value)


def test_from_dict_accepts_finite_created_at_monotonic_round_trip():
    """A finite float must still round-trip after the finiteness guard."""
    original = _ok_response(
        snapshot_summary=WalkSnapshotSummary(
            snapshot_id="sf", items=[], created_at_monotonic=987.654
        )
    )
    restored = ClickElementResponse.from_dict(original.to_dict())
    assert restored == original
    assert restored.snapshot_summary is not None
    assert restored.snapshot_summary.created_at_monotonic == pytest.approx(
        987.654
    )


# ---------------------------------------------------------------------------
# bool-rejection guard coverage (wh-9f3t.13.1)
# ---------------------------------------------------------------------------


def test_from_dict_rejects_created_at_monotonic_as_bool():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["created_at_monotonic"] = True
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "created_at_monotonic" in str(exc_info.value)


def test_from_dict_rejects_display_number_as_bool():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["items"][0]["display_number"] = True
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "display_number" in str(exc_info.value)


def test_from_dict_rejects_monitor_id_as_bool():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["items"][0]["monitor_id"] = False
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "monitor_id" in str(exc_info.value)


def test_from_dict_rejects_bounds_member_as_bool():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"]["items"][0]["bounds"] = [1, True, 3, 4]
    with pytest.raises(ClickElementResponseSchemaError) as exc_info:
        ClickElementResponse.from_dict(payload)
    assert "bounds" in str(exc_info.value)
