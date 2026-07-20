"""Tests for the click-notice IPC schema and wording helper (wh-lstwt).

The click notice is the Logic -> GUI message for a ``click_element``
non-ok outcome (Phase 1 of the voice-element-clicking feature, epic
wh-l4h.1). It is a NEW path, deliberately separate from the
``text_target_rejected`` rejection notice: the click notice has no
``correlation_token`` and no Try-it-anyway retry-on-click semantics
(v5 design doc, "Click notice IPC schema").

Coverage:
  * ``ClickNoticeEvent.to_dict`` / ``from_dict`` exact-inverse round-trip
    for each outcome (not_found, ambiguous, execution_failed) and a
    representative set of execution_failed reasons, including
    ``snapshot_id=None`` and ``matched_names`` list->tuple normalization.
  * ``from_dict`` raises ``ClickNoticeSchemaError`` on every malformed
    shape (non-mapping, missing required field, wrong field type,
    unrecognized outcome, non-string matched_names member) -- never an
    unhandled KeyError / TypeError / AttributeError.
  * ``compose_click_notice_wording`` renders the EXACT v5 string for
    every row of the "User-visible notice wording" table.

The PySide6 widget (``click_notice_toast.py``) is NOT unit-tested here:
it needs a QApplication and is a thin consumer of the wording helper.
The contract test fakes the Logic -> GUI forward by constructing the
event and asserting the pure-data wording output.
"""

from __future__ import annotations

import pytest

from click_notice_toast_wording import compose_click_notice_wording
from services.wheelhouse.shared.click_notice import (
    ClickNoticeEvent,
    ClickNoticeSchemaError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(**overrides) -> ClickNoticeEvent:
    fields = dict(
        outcome="execution_failed",
        reason="disabled",
        matched_name="Submit",
        matched_names=("Submit",),
        spoken_name="submit",
        app_friendly_name="Example App",
        snapshot_id="s1",
        trace_id="trace-abc-123",
    )
    fields.update(overrides)
    return ClickNoticeEvent(**fields)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_not_found():
    original = ClickNoticeEvent(
        outcome="not_found",
        reason=None,
        matched_name=None,
        matched_names=(),
        spoken_name="cancel",
        app_friendly_name="Example App",
        snapshot_id=None,
        trace_id="trace-nf",
    )
    restored = ClickNoticeEvent.from_dict(original.to_dict())
    assert restored == original
    assert restored.matched_names == ()
    assert restored.snapshot_id is None


def test_round_trip_ambiguous_multiple_names():
    original = ClickNoticeEvent(
        outcome="ambiguous",
        reason=None,
        matched_name=None,
        matched_names=("Cancel", "Cancel and exit", "Cancel all"),
        spoken_name="cancel",
        app_friendly_name="Example App",
        snapshot_id="s2",
        trace_id="trace-amb",
    )
    restored = ClickNoticeEvent.from_dict(original.to_dict())
    assert restored == original
    assert restored.matched_names == ("Cancel", "Cancel and exit", "Cancel all")


@pytest.mark.parametrize(
    "reason",
    [
        "disabled",
        "bounds_invalid",
        "foreground_changed",
        "foreground_verification_failed",
        "invoke_com_error",
        "invoke_then_sendinput_failed",
        "sendinput_short",
        "target_moved_offscreen",
        "timeout",
    ],
)
def test_round_trip_execution_failed_reasons(reason: str):
    original = _event(outcome="execution_failed", reason=reason)
    restored = ClickNoticeEvent.from_dict(original.to_dict())
    assert restored == original
    assert restored.reason == reason


def test_round_trip_snapshot_id_none():
    original = _event(snapshot_id=None)
    restored = ClickNoticeEvent.from_dict(original.to_dict())
    assert restored == original
    assert restored.snapshot_id is None


def test_matched_names_list_normalizes_to_tuple():
    payload = _event(outcome="ambiguous", reason=None, matched_name=None).to_dict()
    payload["matched_names"] = ["a", "b"]  # JSON-bridged transport delivers a list
    restored = ClickNoticeEvent.from_dict(payload)
    assert restored.matched_names == ("a", "b")
    assert isinstance(restored.matched_names, tuple)


def test_trace_id_round_trips():
    original = _event(trace_id="unique-trace-id-xyz")
    payload = original.to_dict()
    assert payload["trace_id"] == "unique-trace-id-xyz"
    restored = ClickNoticeEvent.from_dict(payload)
    assert restored.trace_id == "unique-trace-id-xyz"


def test_event_has_no_correlation_token_field():
    """The click notice has no retry-on-click semantics; v5 is explicit
    that it carries no correlation_token (unlike text_target_rejected)."""
    payload = _event().to_dict()
    assert "correlation_token" not in payload
    assert not hasattr(_event(), "correlation_token")


def test_event_is_immutable():
    event = _event()
    with pytest.raises(Exception):
        event.outcome = "not_found"  # type: ignore[misc]


def test_accepts_none_for_optional_strings():
    payload = _event(
        outcome="not_found", reason=None, matched_name=None, snapshot_id=None
    ).to_dict()
    restored = ClickNoticeEvent.from_dict(payload)
    assert restored.reason is None
    assert restored.matched_name is None
    assert restored.snapshot_id is None


# ---------------------------------------------------------------------------
# Closed-set outcome validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome", ["not_found", "ambiguous", "execution_failed"]
)
def test_from_dict_accepts_valid_outcome(outcome: str):
    payload = _event(outcome=outcome).to_dict()
    restored = ClickNoticeEvent.from_dict(payload)
    assert restored.outcome == outcome


def test_from_dict_rejects_unrecognized_outcome():
    payload = _event().to_dict()
    payload["outcome"] = "nonsense"
    with pytest.raises(ClickNoticeSchemaError) as exc_info:
        ClickNoticeEvent.from_dict(payload)
    assert "outcome" in str(exc_info.value)


def test_from_dict_rejects_ok_outcome():
    """'ok' is not in the click-notice closed set -- a click that
    succeeds shows no notice, so 'ok' never travels on this schema."""
    payload = _event().to_dict()
    payload["outcome"] = "ok"
    with pytest.raises(ClickNoticeSchemaError) as exc_info:
        ClickNoticeEvent.from_dict(payload)
    assert "outcome" in str(exc_info.value)


def test_from_dict_leaves_reason_an_open_tag_set():
    """reason is an open str-or-None tag (mirrors click_element.py, which
    leaves its reason value-domain unconstrained)."""
    payload = _event(reason="some_brand_new_executor_tag").to_dict()
    restored = ClickNoticeEvent.from_dict(payload)
    assert restored.reason == "some_brand_new_executor_tag"


# ---------------------------------------------------------------------------
# Validation: top-level structure
# ---------------------------------------------------------------------------


def test_from_dict_rejects_non_mapping():
    with pytest.raises(ClickNoticeSchemaError):
        ClickNoticeEvent.from_dict("not a dict")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "missing_field",
    [
        "outcome",
        "reason",
        "matched_name",
        "matched_names",
        "spoken_name",
        "app_friendly_name",
        "snapshot_id",
        "trace_id",
    ],
)
def test_from_dict_rejects_missing_field(missing_field: str):
    payload = _event().to_dict()
    del payload[missing_field]
    with pytest.raises(ClickNoticeSchemaError) as exc_info:
        ClickNoticeEvent.from_dict(payload)
    assert missing_field in str(exc_info.value)


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("outcome", 42),
        ("spoken_name", None),
        ("app_friendly_name", 1.5),
        ("trace_id", []),
    ],
)
def test_from_dict_rejects_non_string_required_field(field: str, bad_value):
    payload = _event().to_dict()
    payload[field] = bad_value
    with pytest.raises(ClickNoticeSchemaError) as exc_info:
        ClickNoticeEvent.from_dict(payload)
    assert field in str(exc_info.value)


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("reason", 42),
        ("matched_name", []),
        ("snapshot_id", 3.14),
    ],
)
def test_from_dict_rejects_wrong_type_optional_string(field: str, bad_value):
    """reason / matched_name / snapshot_id are str | None: a non-str,
    non-None value is rejected."""
    payload = _event().to_dict()
    payload[field] = bad_value
    with pytest.raises(ClickNoticeSchemaError) as exc_info:
        ClickNoticeEvent.from_dict(payload)
    assert field in str(exc_info.value)


# ---------------------------------------------------------------------------
# Validation: matched_names
# ---------------------------------------------------------------------------


def test_from_dict_rejects_non_list_tuple_matched_names():
    payload = _event().to_dict()
    payload["matched_names"] = 42
    with pytest.raises(ClickNoticeSchemaError) as exc_info:
        ClickNoticeEvent.from_dict(payload)
    assert "matched_names" in str(exc_info.value)


def test_from_dict_rejects_non_string_matched_names_member():
    payload = _event(outcome="ambiguous").to_dict()
    payload["matched_names"] = ["Cancel", 42]
    with pytest.raises(ClickNoticeSchemaError) as exc_info:
        ClickNoticeEvent.from_dict(payload)
    assert "matched_names" in str(exc_info.value)


def test_from_dict_rejects_set_matched_names():
    """A set is iterable but unordered; not in the documented wire shape."""
    payload = _event().to_dict()
    payload["matched_names"] = {"Cancel", "Submit"}
    with pytest.raises(ClickNoticeSchemaError):
        ClickNoticeEvent.from_dict(payload)


def test_from_dict_rejects_arbitrary_iterable_matched_names():
    """The exact-type gate (type(raw) is list/tuple) rejects an arbitrary
    iterable that is neither a list nor a tuple BEFORE any __iter__ runs,
    so a non-schema exception cannot leak past the graceful-degrade
    boundary."""

    iter_called = {"value": False}

    class ArbitraryIterable:
        def __iter__(self):
            iter_called["value"] = True
            return iter(("Cancel",))

    payload = _event().to_dict()
    payload["matched_names"] = ArbitraryIterable()
    with pytest.raises(ClickNoticeSchemaError) as exc_info:
        ClickNoticeEvent.from_dict(payload)
    assert "matched_names" in str(exc_info.value)
    assert iter_called["value"] is False


def test_from_dict_rejects_hostile_list_subclass_matched_names():
    """A list subclass whose __iter__ raises must be rejected as a schema
    error at the exact-type gate before __iter__ is ever called, so the
    RuntimeError never escapes from_dict (mirrors click_element.py)."""

    class HostileList(list):
        def __iter__(self):
            raise RuntimeError("hostile __iter__")

    payload = _event().to_dict()
    payload["matched_names"] = HostileList(["Cancel"])
    with pytest.raises(ClickNoticeSchemaError) as exc_info:
        ClickNoticeEvent.from_dict(payload)
    assert isinstance(exc_info.value, ClickNoticeSchemaError)
    assert "matched_names" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Wording: every row of the v5 "User-visible notice wording" table
# ---------------------------------------------------------------------------


def test_wording_not_found():
    event = _event(
        outcome="not_found",
        reason=None,
        matched_name=None,
        matched_names=(),
        spoken_name="cancel",
    )
    assert compose_click_notice_wording(event) == "No match for 'cancel'."


def test_wording_ambiguous_two_names():
    event = _event(
        outcome="ambiguous",
        reason=None,
        matched_name=None,
        matched_names=("Cancel", "Cancel and exit"),
        spoken_name="cancel",
    )
    assert (
        compose_click_notice_wording(event)
        == "Found 'Cancel' and 'Cancel and exit' -- be more specific."
    )


def test_wording_ambiguous_renders_all_handed_names():
    """wh-9f3t.14.1: Logic owns the trim to notice_max_names BEFORE
    sending the event, so matched_names is already the final list. The
    helper imposes no cap of its own and renders every name it receives.
    Three names handed -> all three appear."""
    event = _event(
        outcome="ambiguous",
        reason=None,
        matched_name=None,
        matched_names=("Cancel", "Cancel and exit", "Cancel all"),
        spoken_name="cancel",
    )
    result = compose_click_notice_wording(event)
    assert "'Cancel'" in result
    assert "'Cancel and exit'" in result
    assert "'Cancel all'" in result
    assert result.endswith(" -- be more specific.")
    assert "  " not in result  # no double space


def test_wording_ambiguous_three_names_exact():
    """The 2+ join is comma-separated with 'and' before the last name,
    so three names read as a natural list."""
    event = _event(
        outcome="ambiguous",
        reason=None,
        matched_name=None,
        matched_names=("Cancel", "Cancel and exit", "Cancel all"),
        spoken_name="cancel",
    )
    assert compose_click_notice_wording(event) == (
        "Found 'Cancel', 'Cancel and exit' and 'Cancel all' "
        "-- be more specific."
    )


def test_wording_execution_failed_disabled():
    event = _event(outcome="execution_failed", reason="disabled", matched_name="Submit")
    assert compose_click_notice_wording(event) == "'Submit' is disabled."


def test_wording_execution_failed_bounds_invalid():
    event = _event(
        outcome="execution_failed", reason="bounds_invalid", matched_name="Submit"
    )
    assert (
        compose_click_notice_wording(event)
        == "Wheelhouse couldn't click 'Submit' -- it may have moved."
    )


def test_wording_execution_failed_foreground_changed():
    event = _event(
        outcome="execution_failed", reason="foreground_changed", matched_name="Submit"
    )
    assert (
        compose_click_notice_wording(event)
        == "Window changed before Wheelhouse could click 'Submit'."
    )


def test_wording_execution_failed_foreground_verification_failed():
    event = _event(
        outcome="execution_failed",
        reason="foreground_verification_failed",
        matched_name="Submit",
    )
    assert compose_click_notice_wording(event) == (
        "Wheelhouse couldn't verify the active window -- if you didn't "
        "switch apps, try clicking again."
    )


def test_wording_execution_failed_invoke_com_error():
    event = _event(
        outcome="execution_failed", reason="invoke_com_error", matched_name="Submit"
    )
    assert (
        compose_click_notice_wording(event)
        == "Wheelhouse couldn't click 'Submit' -- the control did not respond."
    )


def test_wording_execution_failed_invoke_then_sendinput_failed():
    """Same wording as invoke_com_error (v5 table)."""
    event = _event(
        outcome="execution_failed",
        reason="invoke_then_sendinput_failed",
        matched_name="Submit",
    )
    assert (
        compose_click_notice_wording(event)
        == "Wheelhouse couldn't click 'Submit' -- the control did not respond."
    )


def test_wording_execution_failed_sendinput_short():
    """Same wording as invoke_com_error (v5 table)."""
    event = _event(
        outcome="execution_failed", reason="sendinput_short", matched_name="Submit"
    )
    assert (
        compose_click_notice_wording(event)
        == "Wheelhouse couldn't click 'Submit' -- the control did not respond."
    )


def test_wording_execution_failed_dda_unavailable():
    """wh-dda-notice-wording: the control exposes neither an Invoke pattern
    nor a resolvable default action, so pressing it by voice can never work.
    The copy must read permanent (retrying will not help), unlike the
    transient 'did not respond' family."""
    event = _event(
        outcome="execution_failed", reason="dda_unavailable", matched_name="Submit"
    )
    assert (
        compose_click_notice_wording(event)
        == "'Submit' can't be clicked by voice."
    )


def test_wording_execution_failed_dda_no_default_action():
    """wh-dda-notice-wording: same permanent-inability copy as
    dda_unavailable -- the control reports no default action to press."""
    event = _event(
        outcome="execution_failed",
        reason="dda_no_default_action",
        matched_name="Submit",
    )
    assert (
        compose_click_notice_wording(event)
        == "'Submit' can't be clicked by voice."
    )


def test_wording_execution_failed_dda_no_default_action_failed():
    """wh-dda-notice-wording: the DoDefaultAction press was attempted and
    failed -- the delivery-failure analogue of invoke_com_error, so it shares
    that copy (restores the specificity the retired invoke_pattern_unavailable
    tag had)."""
    event = _event(
        outcome="execution_failed",
        reason="dda_no_default_action_failed",
        matched_name="Submit",
    )
    assert (
        compose_click_notice_wording(event)
        == "Wheelhouse couldn't click 'Submit' -- the control did not respond."
    )


def test_wording_execution_failed_dda_no_side_effect_then_sendinput_failed():
    """wh-dda-notice-wording: the no-side-effect DoDefaultAction succeeded but
    the SendInput follow-through failed -- the delivery-failure analogue of
    invoke_then_sendinput_failed, so it shares the invoke_com_error copy."""
    event = _event(
        outcome="execution_failed",
        reason="dda_no_side_effect_then_sendinput_failed",
        matched_name="Submit",
    )
    assert (
        compose_click_notice_wording(event)
        == "Wheelhouse couldn't click 'Submit' -- the control did not respond."
    )


def test_wording_invoke_pattern_unavailable_now_neutral():
    """wh-dda-notice-wording: invoke_pattern_unavailable was retired in
    wh-l4h.1.17 (click_executor.py names no producer for it), so the dead
    alias is removed and the tag renders the NEUTRAL fallback like any
    unknown tag -- not the specific 'did not respond' copy."""
    event = _event(
        outcome="execution_failed",
        reason="invoke_pattern_unavailable",
        matched_name="Submit",
    )
    assert compose_click_notice_wording(event) == "Wheelhouse couldn't click 'Submit'."


def test_wording_execution_failed_target_moved_offscreen():
    event = _event(
        outcome="execution_failed",
        reason="target_moved_offscreen",
        matched_name="Submit",
    )
    assert compose_click_notice_wording(event) == "'Submit' moved off screen."


def test_wording_execution_failed_timeout():
    event = _event(outcome="execution_failed", reason="timeout", matched_name=None)
    assert (
        compose_click_notice_wording(event)
        == "Wheelhouse timed out while clicking."
    )


def test_wording_execution_failed_popup_closed_with_name():
    """wh-n29v.71: an owned #32768 / UIA-Menu popup that closed between the
    walk and the click yields execution_failed:popup_closed. The notice tells
    the user the menu closed before WheelHouse could click the named item."""
    event = _event(
        outcome="execution_failed", reason="popup_closed", matched_name="Copy"
    )
    assert (
        compose_click_notice_wording(event)
        == "The menu closed before Wheelhouse could click 'Copy'."
    )


def test_wording_execution_failed_popup_closed_without_name():
    """When no matched name is available, the popup_closed notice degrades to
    a name-less form consistent with the sibling name-using branches (which
    fall through to the neutral generic string on an empty name). Here we use
    a sensible popup-specific name-less fallback."""
    event = _event(
        outcome="execution_failed", reason="popup_closed", matched_name=None
    )
    assert (
        compose_click_notice_wording(event)
        == "The menu closed before Wheelhouse could click it."
    )


@pytest.mark.parametrize("matched_name", [None, "", "Submit"])
def test_wording_execution_failed_automation_unavailable(matched_name):
    """wh-n29v.74.1 (deepseek reviewer_2): when the by-name click / overlay
    short-circuits because the IUIAutomation root could not be created (COM /
    UIAutomationCore unavailable on a degraded / headless / locked-down host),
    the Input side now emits reason='automation_unavailable' -- a DISTINCT tag
    from 'disabled_by_config'. The user notice must say the feature is
    unavailable on this system, NOT tell the user to check config.toml [click]
    (clicking IS enabled in config; the config file is fine). The cause is the
    machine, not the config, so the wording embeds no control name and is the
    same for every matched_name value."""
    event = _event(
        outcome="execution_failed",
        reason="automation_unavailable",
        matched_name=matched_name,
    )
    assert (
        compose_click_notice_wording(event)
        == "Voice clicking is unavailable on this system."
    )


# ---------------------------------------------------------------------------
# Logic-synthesised reasons (wh-g4oma): explicit, name-independent wording
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (
            "disabled_by_config",
            "Voice clicking is disabled -- check config.toml [click].",
        ),
        (
            "snapshot_expired",
            "The numbered overlay has expired -- say the click command again "
            "to get fresh numbers.",
        ),
        (
            "malformed_response",
            "Something went wrong on the click command -- check the log for "
            "details and try again.",
        ),
        (
            # wh-9f3t.59.3: malformed_query is an internal/IPC-corruption
            # error of the same class as malformed_response and shares its
            # exact copy.
            "malformed_query",
            "Something went wrong on the click command -- check the log for "
            "details and try again.",
        ),
        (
            "send_request_failed",
            "Wheelhouse couldn't send the click request.",
        ),
    ],
)
@pytest.mark.parametrize("matched_name", [None, "", "Submit"])
def test_wording_logic_synthesised_reason_is_name_independent(
    reason: str, expected: str, matched_name: str | None
):
    """wh-g4oma / wh-9f3t.57.1 / wh-9f3t.59.3: these internal-error reasons
    render fixed copy that names no control. Crossing each reason with
    matched_name in [None, "", "Submit"] asserts the SAME constant string in
    all three cases, proving the wording does not depend on the name in
    EITHER direction. The single-value (None-only) version of this test
    would not catch a future refactor that accidentally added an `and name`
    guard to one of these branches -- and the neutral fallback in the same
    function DOES branch on name, so that mistake is easy to make here.
    malformed_query shares malformed_response's copy (same error class)."""
    event = _event(
        outcome="execution_failed",
        reason=reason,
        matched_name=matched_name,
    )
    assert compose_click_notice_wording(event) == expected


# ---------------------------------------------------------------------------
# Degrade branches (wh-9f3t.14.2, .14.3, .14.4)
# ---------------------------------------------------------------------------


def test_wording_unknown_execution_failed_reason_is_neutral_with_name():
    """wh-9f3t.14.2: an unrecognized reason that is not in the v5 table
    must NOT borrow the invoke_com_error wording, which asserts a specific
    wrong cause ('the control did not respond'). It uses neutral wording
    naming no cause.

    The example reason is a synthetic tag that no slice will ever give
    specific wording, so this test stays valid after wh-g4oma adds the
    Logic-synthesised reasons (disabled_by_config, snapshot_expired,
    malformed_response). It deliberately does NOT use snapshot_expired:
    that is a real reason owned end-to-end by wh-g4oma, which will give it
    specific wording -- using it here would assert behavior wh-g4oma is
    expected to change (codex reviewer_1 finding wh-9f3t.15.2)."""
    event = _event(
        outcome="execution_failed",
        reason="unknown_future_executor_tag",
        matched_name="Submit",
    )
    result = compose_click_notice_wording(event)
    assert result == "Wheelhouse couldn't click 'Submit'."
    assert "did not respond" not in result


def test_wording_unknown_execution_failed_reason_neutral_without_name():
    """When no matched_name is available, the neutral fallback is the
    generic 'couldn't complete the click' string -- still no cause. Uses a
    synthetic never-real reason tag for the same durability reason as the
    with-name case above (codex reviewer_1 finding wh-9f3t.15.2)."""
    event = _event(
        outcome="execution_failed",
        reason="unknown_future_executor_tag",
        matched_name=None,
    )
    result = compose_click_notice_wording(event)
    assert result == "Wheelhouse couldn't complete the click."
    assert "did not respond" not in result


def test_wording_none_execution_failed_reason_is_neutral():
    """A None reason on an execution_failed event is also unknown to the
    table; it must take the neutral fallback, not the invoke_com_error
    wording."""
    event = _event(
        outcome="execution_failed",
        reason=None,
        matched_name="Submit",
    )
    assert (
        compose_click_notice_wording(event)
        == "Wheelhouse couldn't click 'Submit'."
    )


@pytest.mark.parametrize(
    "reason",
    [
        "disabled",
        "bounds_invalid",
        "foreground_changed",
        "invoke_com_error",
        "invoke_then_sendinput_failed",
        "sendinput_short",
        "target_moved_offscreen",
        "dda_unavailable",
        "dda_no_default_action",
        "dda_no_default_action_failed",
        "dda_no_side_effect_then_sendinput_failed",
        "dda_unavailable_then_sendinput_failed",
        "dda_no_default_action_then_sendinput_failed",
        "click_point_obstructed",
    ],
)
def test_wording_empty_matched_name_with_name_using_reason_is_generic(reason: str):
    """reviewer_2 (deepseek) finding wh-9f3t.16.1: an empty matched_name is
    schema-valid (str-or-None accepts ""). For the reasons whose wording
    embeds the name, an empty name must fall through to the generic neutral
    string, NOT emit quoted-empty-string text like "'' is disabled." Logic
    sends None rather than "" today, but the helper runs in the GUI process
    across the IPC boundary and must stay robust against any schema-valid
    input."""
    event = _event(
        outcome="execution_failed",
        reason=reason,
        matched_name="",
    )
    result = compose_click_notice_wording(event)
    assert result == "Wheelhouse couldn't complete the click."
    assert "''" not in result


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (
            "foreground_verification_failed",
            "Wheelhouse couldn't verify the active window -- if you didn't "
            "switch apps, try clicking again.",
        ),
        ("timeout", "Wheelhouse timed out while clicking."),
    ],
)
def test_wording_empty_matched_name_keeps_name_independent_wording(
    reason: str, expected: str
):
    """The foreground-verification and timeout branches do not use the
    matched name, so an empty matched_name must NOT collapse them to the
    generic string -- they keep their specific wording (reviewer_2 finding
    wh-9f3t.16.1, refinement on the proposed fix)."""
    event = _event(
        outcome="execution_failed",
        reason=reason,
        matched_name="",
    )
    assert compose_click_notice_wording(event) == expected


def test_wording_ambiguous_zero_names_no_double_space():
    """wh-9f3t.14.3: the schema accepts an empty matched_names, so the
    ambiguous path must degrade gracefully -- a generic string with no
    double space, not 'Found  -- be more specific.'"""
    event = _event(
        outcome="ambiguous",
        reason=None,
        matched_name=None,
        matched_names=(),
        spoken_name="cancel",
    )
    result = compose_click_notice_wording(event)
    assert result == "Found multiple matches -- be more specific."
    assert "  " not in result


def test_wording_ambiguous_single_name_reads_naturally():
    """wh-9f3t.14.3: a single name must read naturally, not produce an odd
    'Found 'X' and  -- ...' string."""
    event = _event(
        outcome="ambiguous",
        reason=None,
        matched_name=None,
        matched_names=("Cancel",),
        spoken_name="cancel",
    )
    result = compose_click_notice_wording(event)
    assert result == "Found 'Cancel' -- be more specific."
    assert "  " not in result


def test_wording_unknown_outcome_fallback_no_crash():
    """wh-9f3t.14.4: the helper renders a usable string rather than raise
    for an outcome value outside the closed set. The schema rejects such
    values, so this is reached only if a caller constructs the dataclass
    directly with a bad outcome (the dataclass itself does no validation)."""
    event = _event(outcome="some_future_outcome", reason=None, matched_name=None)
    result = compose_click_notice_wording(event)
    assert isinstance(result, str)
    assert result == "Wheelhouse couldn't complete the click."


def test_wording_overlay_numbers_changed_is_exact():
    """wh-overlay-fixqueue-review.2: the renumber guard's notice tells the
    user the badges changed and to re-check -- name-independent (the spoken
    text is a number, not a control name)."""
    event = _event(
        outcome="execution_failed",
        reason="overlay_numbers_changed",
        matched_name=None,
        matched_names=(),
        spoken_name="5",
    )
    assert compose_click_notice_wording(event) == (
        "The numbers just updated -- check the number and say it again."
    )


def test_wording_execution_failed_dda_unavailable_then_sendinput_failed():
    """wh-explorer-navpane-click: both press patterns were structurally
    absent (nothing fired) and the structural coordinate fallback's click did
    not land -- a transient delivery failure, so it shares the
    invoke_com_error copy, NOT the permanent 'can't be clicked' copy."""
    event = _event(
        outcome="execution_failed",
        reason="dda_unavailable_then_sendinput_failed",
        matched_name="Documents (pinned)",
    )
    assert compose_click_notice_wording(event) == (
        "Wheelhouse couldn't click 'Documents (pinned)' -- the control did "
        "not respond."
    )


def test_wording_execution_failed_dda_no_default_action_then_sendinput_failed():
    """wh-explorer-navpane-click: the empty-default-action twin of the test
    above -- same transient delivery-failure copy."""
    event = _event(
        outcome="execution_failed",
        reason="dda_no_default_action_then_sendinput_failed",
        matched_name="Documents (pinned)",
    )
    assert compose_click_notice_wording(event) == (
        "Wheelhouse couldn't click 'Documents (pinned)' -- the control did "
        "not respond."
    )


def test_wording_execution_failed_click_point_obstructed():
    """wh-explorer-navpane-click.1.1: the pre-send hit-test found a different
    top-level window under the click point (an always-on-top occluder), or
    could not verify the point at all. The copy names the likely cause so a
    hands-free user knows what to move."""
    event = _event(
        outcome="execution_failed",
        reason="click_point_obstructed",
        matched_name="Documents (pinned)",
    )
    assert compose_click_notice_wording(event) == (
        "Wheelhouse couldn't click 'Documents (pinned)' -- another window "
        "may be covering it."
    )
