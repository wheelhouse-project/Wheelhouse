"""Tests for the editor_rebuilt notification schema (wh-g2-refactor.14).

The GUI Process emits this notification when ``_rebuild_persistent_editor``
fires (foreground-transfer failure or focus-confirmed-poll exhaustion).
Logic receives it and fans out ``editor_rebuilt`` failures to every
pending insert/retract future whose stored generation is less than or
equal to the retired generation. Section 6 of the G2 design refinements
is the authoritative reference.

Coverage:
  * Notification round-trips.
  * Rejects negative generations and non-int generations.
  * Rejects ``new_generation <= old_generation`` (the rebuild always
    advances the counter).
  * Allows empty ``reason`` but rejects non-string reasons.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.editor_rebuilt import (
    ACTION_NAME,
    EditorRebuiltNotification,
    EditorRebuiltSchemaError,
)


def test_action_name_constant_matches_design_doc():
    assert ACTION_NAME == "editor_rebuilt"


def test_notification_round_trip():
    n = EditorRebuiltNotification(
        old_generation=3,
        new_generation=4,
        reason="foreground_transfer_failed",
    )
    restored = EditorRebuiltNotification.from_dict(n.to_dict())
    assert restored == n


def test_notification_to_dict_shape():
    n = EditorRebuiltNotification(
        old_generation=0,
        new_generation=1,
        reason="modern_standby_resume",
    )
    d = n.to_dict()
    assert d["action"] == ACTION_NAME
    assert d["old_generation"] == 0
    assert d["new_generation"] == 1
    assert d["reason"] == "modern_standby_resume"


def test_notification_is_immutable():
    n = EditorRebuiltNotification(
        old_generation=0,
        new_generation=1,
        reason="r",
    )
    with pytest.raises(Exception):
        n.reason = "x"  # type: ignore[misc]


def test_notification_allows_zero_old_generation():
    """The first rebuild advances 0 -> 1."""
    n = EditorRebuiltNotification(
        old_generation=0,
        new_generation=1,
        reason="r",
    )
    assert n.old_generation == 0


def test_notification_rejects_negative_old_generation():
    with pytest.raises(EditorRebuiltSchemaError):
        EditorRebuiltNotification(
            old_generation=-1,
            new_generation=0,
            reason="r",
        )


def test_notification_rejects_negative_new_generation():
    with pytest.raises(EditorRebuiltSchemaError):
        EditorRebuiltNotification(
            old_generation=0,
            new_generation=-1,
            reason="r",
        )


def test_notification_requires_new_greater_than_old():
    """A rebuild always advances the generation; same or smaller is malformed."""
    with pytest.raises(EditorRebuiltSchemaError):
        EditorRebuiltNotification(
            old_generation=2,
            new_generation=2,
            reason="r",
        )


def test_notification_rejects_new_less_than_old():
    with pytest.raises(EditorRebuiltSchemaError):
        EditorRebuiltNotification(
            old_generation=5,
            new_generation=4,
            reason="r",
        )


def test_notification_rejects_generation_gap():
    """Round 1 / deepseek finding wh-g2-refactor.30.2: the validator
    enforces ``new == old + 1`` exactly, not the looser ``new > old``.
    A gap (e.g. old=0, new=2) is a GUI-side double-bump bug that
    would strand any future at the skipped intermediate generation;
    the boundary check fails loud per wh-uf54.
    """
    with pytest.raises(EditorRebuiltSchemaError):
        EditorRebuiltNotification(
            old_generation=0,
            new_generation=2,
            reason="r",
        )
    with pytest.raises(EditorRebuiltSchemaError):
        EditorRebuiltNotification(
            old_generation=3,
            new_generation=7,
            reason="r",
        )


def test_notification_rejects_non_int_generations():
    with pytest.raises(EditorRebuiltSchemaError):
        EditorRebuiltNotification(
            old_generation="0",  # type: ignore[arg-type]
            new_generation=1,
            reason="r",
        )


def test_notification_rejects_bool_generations():
    with pytest.raises(EditorRebuiltSchemaError):
        EditorRebuiltNotification(
            old_generation=False,  # type: ignore[arg-type]
            new_generation=1,
            reason="r",
        )


def test_notification_rejects_non_string_reason():
    with pytest.raises(EditorRebuiltSchemaError):
        EditorRebuiltNotification(
            old_generation=0,
            new_generation=1,
            reason=42,  # type: ignore[arg-type]
        )


def test_notification_allows_empty_reason():
    """Empty reason is valid; callers should still pass a string."""
    n = EditorRebuiltNotification(
        old_generation=0,
        new_generation=1,
        reason="",
    )
    assert n.reason == ""


def test_notification_from_dict_rejects_wrong_action():
    payload = {
        "action": "not_rebuilt",
        "old_generation": 0,
        "new_generation": 1,
        "reason": "r",
    }
    with pytest.raises(EditorRebuiltSchemaError):
        EditorRebuiltNotification.from_dict(payload)


def test_notification_from_dict_rejects_missing_fields():
    base = {
        "action": ACTION_NAME,
        "old_generation": 0,
        "new_generation": 1,
        "reason": "r",
    }
    for missing in ("old_generation", "new_generation", "reason"):
        payload = dict(base)
        del payload[missing]
        with pytest.raises(EditorRebuiltSchemaError):
            EditorRebuiltNotification.from_dict(payload)


def test_notification_from_dict_rejects_non_mapping():
    with pytest.raises(EditorRebuiltSchemaError):
        EditorRebuiltNotification.from_dict([1, 2, 3])  # type: ignore[arg-type]
