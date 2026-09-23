"""Schema tests for the pattern_manager_tree_changed GUI -> Logic event.

Bead wh-overlay-rewalk-after-filter, contract point C5: the new event reuses
the existing GUI -> Logic channel and the existing schema style -- a frozen
dataclass whose ``from_dict`` validates every key ``to_dict`` writes and
raises one schema error the Logic side catches through ``safe_parse``.

These tests mirror ``test_snapshot_item_clicked.py`` and
``test_overlay_state_changed.py``: every required key, every wrong type, and
the ``bool``-is-an-``int`` trap.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.pattern_manager_tree_changed import (
    ACTION_NAME,
    PatternManagerTreeChangedEvent,
    PatternManagerTreeChangedSchemaError,
)


def _payload(**overrides):
    payload = {
        "action": ACTION_NAME,
        "hwnd": 123456,
        "sequence": 1,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Round trip.
# ---------------------------------------------------------------------------


def test_action_name_is_not_pm_prefixed():
    # The Logic command listener routes every action starting with "pm_" to
    # _handle_pattern_manager_action BEFORE the handler table, so a pm_-
    # prefixed name would never reach this event's handler (main.py:11249).
    assert not ACTION_NAME.startswith("pm_")
    assert ACTION_NAME == "pattern_manager_tree_changed"


def test_to_dict_round_trips_through_from_dict():
    event = PatternManagerTreeChangedEvent(hwnd=987654, sequence=7)
    assert PatternManagerTreeChangedEvent.from_dict(event.to_dict()) == event


def test_to_dict_carries_the_action_key():
    event = PatternManagerTreeChangedEvent(hwnd=1, sequence=1)
    assert event.to_dict()["action"] == ACTION_NAME


def test_event_is_frozen():
    event = PatternManagerTreeChangedEvent(hwnd=1, sequence=1)
    with pytest.raises(Exception):
        event.hwnd = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Payload shape.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [None, 5, "text", ["action"], object()])
def test_non_mapping_payload_rejected(payload):
    with pytest.raises(PatternManagerTreeChangedSchemaError):
        PatternManagerTreeChangedEvent.from_dict(payload)


def test_missing_action_rejected():
    payload = _payload()
    del payload["action"]
    with pytest.raises(PatternManagerTreeChangedSchemaError):
        PatternManagerTreeChangedEvent.from_dict(payload)


def test_wrong_action_rejected():
    with pytest.raises(PatternManagerTreeChangedSchemaError):
        PatternManagerTreeChangedEvent.from_dict(
            _payload(action="overlay_state_changed")
        )


# ---------------------------------------------------------------------------
# hwnd.
# ---------------------------------------------------------------------------


def test_missing_hwnd_rejected():
    payload = _payload()
    del payload["hwnd"]
    with pytest.raises(PatternManagerTreeChangedSchemaError):
        PatternManagerTreeChangedEvent.from_dict(payload)


@pytest.mark.parametrize("value", ["123", 1.5, None, [1], {"a": 1}])
def test_non_int_hwnd_rejected(value):
    with pytest.raises(PatternManagerTreeChangedSchemaError):
        PatternManagerTreeChangedEvent.from_dict(_payload(hwnd=value))


def test_bool_hwnd_rejected():
    # bool subclasses int; True must not pass as a window handle.
    with pytest.raises(PatternManagerTreeChangedSchemaError):
        PatternManagerTreeChangedEvent.from_dict(_payload(hwnd=True))


@pytest.mark.parametrize("value", [0, -1, -123456])
def test_non_positive_hwnd_rejected(value):
    # A window handle is never 0 or negative; 0 is the "no window" value the
    # Logic side uses, so it must not arrive on the wire.
    with pytest.raises(PatternManagerTreeChangedSchemaError):
        PatternManagerTreeChangedEvent.from_dict(_payload(hwnd=value))


def test_large_hwnd_accepted():
    # 64-bit window handles are ordinary here; nothing may truncate them.
    event = PatternManagerTreeChangedEvent.from_dict(
        _payload(hwnd=0x7FFFFFFFFFFF)
    )
    assert event.hwnd == 0x7FFFFFFFFFFF


# ---------------------------------------------------------------------------
# sequence.
# ---------------------------------------------------------------------------


def test_missing_sequence_rejected():
    payload = _payload()
    del payload["sequence"]
    with pytest.raises(PatternManagerTreeChangedSchemaError):
        PatternManagerTreeChangedEvent.from_dict(payload)


@pytest.mark.parametrize("value", ["1", 1.5, None, [1]])
def test_non_int_sequence_rejected(value):
    with pytest.raises(PatternManagerTreeChangedSchemaError):
        PatternManagerTreeChangedEvent.from_dict(_payload(sequence=value))


def test_bool_sequence_rejected():
    with pytest.raises(PatternManagerTreeChangedSchemaError):
        PatternManagerTreeChangedEvent.from_dict(_payload(sequence=True))


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_sequence_rejected(value):
    # The GUI bumps the counter BEFORE it sends, so the first event is 1.
    with pytest.raises(PatternManagerTreeChangedSchemaError):
        PatternManagerTreeChangedEvent.from_dict(_payload(sequence=value))


# ---------------------------------------------------------------------------
# The Logic side never sees a raw exception.
# ---------------------------------------------------------------------------


def test_schema_error_is_a_value_error():
    # safe_parse catches ValueError; the schema error must be one.
    assert issubclass(PatternManagerTreeChangedSchemaError, ValueError)


def test_hostile_mapping_raises_the_schema_error_not_the_raw_one():
    class Hostile(dict):
        def __getitem__(self, key):
            raise KeyError("boom")

    payload = Hostile(_payload())
    with pytest.raises(PatternManagerTreeChangedSchemaError):
        PatternManagerTreeChangedEvent.from_dict(payload)


def test_safe_parse_returns_none_on_a_bad_payload():
    from services.wheelhouse.shared.ipc_schema_validation import safe_parse

    assert safe_parse(
        PatternManagerTreeChangedEvent.from_dict,
        {"action": ACTION_NAME},
        log_label=ACTION_NAME,
    ) is None
