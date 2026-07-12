"""Unit tests for the voice-element-clicking dataclasses (wh-kxgcg).

Verifies construction, field presence, and frozen immutability for the five
pure-data shapes defined in services/wheelhouse/ui/element_types.py.
"""

import dataclasses
import pickle

import pytest

from ui.element_types import (
    ElementMatch,
    ElementQuery,
    WalkSnapshot,
    WalkSnapshotSummary,
    WalkSnapshotSummaryItem,
)


def _make_element_match() -> ElementMatch:
    return ElementMatch(
        item_id="m1",
        display_number=1,
        name="Cancel",
        role="Button",
        bounds=(10, 20, 80, 30),
        monitor_id=0,
        score=0.92,
        is_eligible=True,
        source="uia",
        invoke_supported=True,
        is_enabled=True,
        control_ref=object(),
    )


def _make_summary_item() -> WalkSnapshotSummaryItem:
    return WalkSnapshotSummaryItem(
        item_id="m1",
        display_number=1,
        name="Cancel",
        role="Button",
        bounds=(10, 20, 80, 30),
        monitor_id=0,
    )


def test_element_query_construction_and_fields() -> None:
    query = ElementQuery(
        name="cancel",
        role="Button",
        ordinal=2,
        spatial="near the email field",
        raw_utterance="click the second cancel button",
    )
    assert query.name == "cancel"
    assert query.role == "Button"
    assert query.ordinal == 2
    assert query.spatial == "near the email field"
    assert query.raw_utterance == "click the second cancel button"


def test_element_query_accepts_none_optionals() -> None:
    query = ElementQuery(
        name="submit",
        role=None,
        ordinal=None,
        spatial=None,
        raw_utterance="click submit",
    )
    assert query.role is None
    assert query.ordinal is None
    assert query.spatial is None


def test_element_query_frozen() -> None:
    query = ElementQuery(
        name="cancel", role=None, ordinal=None, spatial=None, raw_utterance="cancel"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        query.name = "submit"  # type: ignore[misc]


def test_element_match_construction_and_fields() -> None:
    match = _make_element_match()
    assert match.item_id == "m1"
    assert match.display_number == 1
    assert match.name == "Cancel"
    assert match.role == "Button"
    assert match.bounds == (10, 20, 80, 30)
    assert match.monitor_id == 0
    assert match.score == pytest.approx(0.92)
    assert match.is_eligible is True
    assert match.source == "uia"
    assert match.invoke_supported is True
    assert match.is_enabled is True
    assert match.control_ref is not None


def test_element_match_frozen() -> None:
    match = _make_element_match()
    with pytest.raises(dataclasses.FrozenInstanceError):
        match.score = 0.1  # type: ignore[misc]


def test_element_match_control_type_id_defaults_to_zero() -> None:
    """wh-l4h.1.12: control_type_id is a field with a 0 default so construction
    sites that predate the field keep compiling. 0 is a sentinel no real UIA
    control uses."""
    match = _make_element_match()  # _make_element_match does not pass the field
    assert match.control_type_id == 0
    field_names = {f.name for f in dataclasses.fields(ElementMatch)}
    assert "control_type_id" in field_names


def test_element_match_control_type_id_round_trips() -> None:
    """The numeric UIA control-type id is carried verbatim when supplied."""
    match = dataclasses.replace(_make_element_match(), control_type_id=50026)
    assert match.control_type_id == 50026


def test_walk_snapshot_construction_and_holds_matches() -> None:
    matches = [_make_element_match(), _make_element_match()]
    snapshot = WalkSnapshot(
        snapshot_id="s1",
        matches=matches,
        created_at_monotonic=123.456,
        foreground_window=4242,
        foreground_pid=1000,
        foreground_process_name="notepad.exe",
        foreground_window_creation_time=987654321,
        cursor_at_walk=(500, 600),
        cursor_monitor_id=0,
    )
    assert snapshot.snapshot_id == "s1"
    assert len(snapshot.matches) == 2
    assert all(isinstance(m, ElementMatch) for m in snapshot.matches)
    assert snapshot.created_at_monotonic == pytest.approx(123.456)
    assert snapshot.foreground_window == 4242
    assert snapshot.foreground_pid == 1000
    assert snapshot.foreground_process_name == "notepad.exe"
    assert snapshot.foreground_window_creation_time == 987654321
    assert snapshot.cursor_at_walk == (500, 600)
    assert snapshot.cursor_monitor_id == 0


def test_walk_snapshot_frozen() -> None:
    snapshot = WalkSnapshot(
        snapshot_id="s1",
        matches=[],
        created_at_monotonic=1.0,
        foreground_window=1,
        foreground_pid=1,
        foreground_process_name="x.exe",
        foreground_window_creation_time=1,
        cursor_at_walk=(0, 0),
        cursor_monitor_id=0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.snapshot_id = "s2"  # type: ignore[misc]


def test_walk_snapshot_summary_construction_and_holds_items() -> None:
    items = [_make_summary_item(), _make_summary_item()]
    summary = WalkSnapshotSummary(
        snapshot_id="s1",
        items=items,
        created_at_monotonic=123.456,
    )
    assert summary.snapshot_id == "s1"
    assert len(summary.items) == 2
    assert all(isinstance(i, WalkSnapshotSummaryItem) for i in summary.items)
    assert summary.created_at_monotonic == pytest.approx(123.456)


def test_walk_snapshot_summary_frozen() -> None:
    summary = WalkSnapshotSummary(
        snapshot_id="s1", items=[], created_at_monotonic=1.0
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        summary.snapshot_id = "s2"  # type: ignore[misc]


def test_walk_snapshot_summary_item_construction_and_fields() -> None:
    item = _make_summary_item()
    assert item.item_id == "m1"
    assert item.display_number == 1
    assert item.name == "Cancel"
    assert item.role == "Button"
    assert item.bounds == (10, 20, 80, 30)
    assert item.monitor_id == 0


def test_walk_snapshot_summary_item_frozen() -> None:
    item = _make_summary_item()
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.item_id = "m2"  # type: ignore[misc]


# ---- Review hardening (wh-9f3t.1 reviewer_0 findings) --------------


def test_walk_snapshot_summary_pickle_round_trip() -> None:
    """wh-9f3t.1.1: WalkSnapshotSummary / WalkSnapshotSummaryItem are the
    plain-data shapes that cross the Input -> Logic -> GUI process boundary.
    A pickle round-trip must reproduce an equal object. This locks the
    boundary contract: a later field addition that is not picklable (e.g. a
    live COM handle) fails here instead of only at runtime in the GUI process.
    """
    summary = WalkSnapshotSummary(
        snapshot_id="s1",
        items=[_make_summary_item(), _make_summary_item()],
        created_at_monotonic=123.456,
    )
    restored = pickle.loads(pickle.dumps(summary))
    assert restored == summary
    assert all(isinstance(i, WalkSnapshotSummaryItem) for i in restored.items)


def test_summary_types_exact_field_set_and_exclude_control_ref() -> None:
    """wh-9f3t.1.2: lock the cross-boundary invariant by asserting the exact
    field set of each summary type (construction tests tolerate an added
    field; this does not) and confirming control_ref -- the Input-local COM
    handle -- never appears on a boundary-crossing type. control_ref must
    stay on ElementMatch, which is Input-process-local.
    """
    item_fields = {f.name for f in dataclasses.fields(WalkSnapshotSummaryItem)}
    assert item_fields == {
        "item_id",
        "display_number",
        "name",
        "role",
        "bounds",
        "monitor_id",
    }
    summary_fields = {f.name for f in dataclasses.fields(WalkSnapshotSummary)}
    assert summary_fields == {"snapshot_id", "items", "created_at_monotonic"}
    assert "control_ref" not in item_fields
    assert "control_ref" not in summary_fields
    assert "control_ref" in {f.name for f in dataclasses.fields(ElementMatch)}
