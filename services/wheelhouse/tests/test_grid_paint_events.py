"""Tests for the three grid Logic -> GUI paint event schemas.

Bead ``wh-grid-state-machine`` under the ``wh-mouse-grid`` molecule; spec
``docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md`` ("Logic
sends 'paint this rectangle as a grid' / 'paint a pin at this point' /
'clear'; the GUI draws and decides nothing").

The three schemas mirror the numbered overlay's ``PaintOverlayEvent`` /
``ClearOverlayEvent`` precedent, so this file mirrors
``tests/test_paint_overlay.py`` and ``tests/test_clear_overlay.py``:

  * Round-trip (``to_dict`` -> ``from_dict``); ``to_dict`` carries the
    action key.
  * ``from_dict`` raises the module's typed SchemaError on every
    malformed shape (not a mapping, missing/wrong action, missing or
    non-int coordinate, a ``bool`` supplied for an int field, a
    non-positive size) -- never an unhandled KeyError / TypeError /
    AttributeError.
  * Negative coordinates round-trip: a monitor left of or above the
    primary has a negative virtual-desktop origin.
"""

from __future__ import annotations

import dataclasses

import pytest

from services.wheelhouse.shared.clear_grid import (
    ACTION_NAME as CLEAR_GRID_ACTION,
    ClearGridEvent,
    ClearGridEventSchemaError,
)
from services.wheelhouse.shared.paint_grid import (
    ACTION_NAME as PAINT_GRID_ACTION,
    PaintGridEvent,
    PaintGridEventSchemaError,
)
from services.wheelhouse.shared.paint_grid_pin import (
    ACTION_NAME as PAINT_GRID_PIN_ACTION,
    PaintGridPinEvent,
    PaintGridPinEventSchemaError,
)


def make_paint_grid() -> PaintGridEvent:
    return PaintGridEvent(
        monitor_left=0,
        monitor_top=0,
        monitor_width=1920,
        monitor_height=1080,
        left=640,
        top=360,
        width=640,
        height=360,
    )


# ===========================================================================
# PaintGridEvent
# ===========================================================================


def test_paint_grid_round_trip():
    evt = make_paint_grid()
    payload = evt.to_dict()
    assert payload["action"] == PAINT_GRID_ACTION
    assert payload["monitor_width"] == 1920
    assert payload["left"] == 640
    assert PaintGridEvent.from_dict(payload) == evt


def test_paint_grid_round_trips_negative_virtual_desktop_coordinates():
    """A monitor left of and above the primary has a negative origin."""

    evt = PaintGridEvent(
        monitor_left=-3840,
        monitor_top=-300,
        monitor_width=3840,
        monitor_height=2160,
        left=-2560,
        top=420,
        width=1280,
        height=720,
    )
    assert PaintGridEvent.from_dict(evt.to_dict()) == evt


def test_paint_grid_action_name():
    assert PAINT_GRID_ACTION == "paint_grid"


def test_paint_grid_not_a_mapping_raises():
    with pytest.raises(PaintGridEventSchemaError):
        PaintGridEvent.from_dict("not a mapping")


def test_paint_grid_missing_action_raises():
    payload = make_paint_grid().to_dict()
    del payload["action"]
    with pytest.raises(PaintGridEventSchemaError):
        PaintGridEvent.from_dict(payload)


def test_paint_grid_wrong_action_raises():
    payload = make_paint_grid().to_dict()
    payload["action"] = "paint_overlay"
    with pytest.raises(PaintGridEventSchemaError):
        PaintGridEvent.from_dict(payload)


@pytest.mark.parametrize(
    "field",
    [
        "monitor_left",
        "monitor_top",
        "monitor_width",
        "monitor_height",
        "left",
        "top",
        "width",
        "height",
    ],
)
def test_paint_grid_missing_field_raises(field):
    payload = make_paint_grid().to_dict()
    del payload[field]
    with pytest.raises(PaintGridEventSchemaError):
        PaintGridEvent.from_dict(payload)


@pytest.mark.parametrize("field", ["monitor_left", "left", "width"])
def test_paint_grid_non_int_field_raises(field):
    payload = make_paint_grid().to_dict()
    payload[field] = "640"
    with pytest.raises(PaintGridEventSchemaError):
        PaintGridEvent.from_dict(payload)


@pytest.mark.parametrize("field", ["monitor_top", "top", "height"])
def test_paint_grid_bool_for_an_int_field_raises(field):
    """bool is a subclass of int; it must not slip through as 0 or 1."""

    payload = make_paint_grid().to_dict()
    payload[field] = True
    with pytest.raises(PaintGridEventSchemaError):
        PaintGridEvent.from_dict(payload)


@pytest.mark.parametrize(
    "field", ["monitor_width", "monitor_height", "width", "height"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_paint_grid_non_positive_size_raises(field, value):
    """A grid with no pixels in it is not paintable."""

    payload = make_paint_grid().to_dict()
    payload[field] = value
    with pytest.raises(PaintGridEventSchemaError):
        PaintGridEvent.from_dict(payload)


def test_paint_grid_constructor_rejects_a_non_positive_size():
    """The producer side fails loudly too, not only the consumer side."""

    with pytest.raises(PaintGridEventSchemaError):
        PaintGridEvent(
            monitor_left=0,
            monitor_top=0,
            monitor_width=1920,
            monitor_height=1080,
            left=0,
            top=0,
            width=0,
            height=100,
        ).to_dict()


@pytest.mark.parametrize(
    "field", ["monitor_left", "monitor_top", "left", "top"]
)
def test_paint_grid_to_dict_rejects_a_non_int_coordinate(field):
    """wh-mouse-grid.1.23: to_dict validates the COORDINATE fields too.

    to_dict's docstring promises a bad producer fails before the GUI drops
    the event, but it only checked the size fields; a coordinate carrying a
    string serialized fine, the GUI's safe_parse dropped it, and the paint
    manager kept showing the PREVIOUS grid cell while Logic's state had
    advanced -- a later "click" then lands in the new cell the user never
    saw. Producer and consumer must validate the same set.
    """

    evt = dataclasses.replace(make_paint_grid(), **{field: "bad-coordinate"})
    with pytest.raises(PaintGridEventSchemaError):
        evt.to_dict()


@pytest.mark.parametrize(
    "field", ["monitor_left", "monitor_top", "left", "top"]
)
def test_paint_grid_to_dict_rejects_a_bool_coordinate(field):
    """bool is a subclass of int; the producer must not emit one either."""

    evt = dataclasses.replace(make_paint_grid(), **{field: True})
    with pytest.raises(PaintGridEventSchemaError):
        evt.to_dict()


# ===========================================================================
# PaintGridPinEvent
# ===========================================================================


def test_paint_grid_pin_round_trip():
    evt = PaintGridPinEvent(x=100, y=250)
    payload = evt.to_dict()
    assert payload["action"] == PAINT_GRID_PIN_ACTION
    assert payload["x"] == 100
    assert payload["y"] == 250
    assert PaintGridPinEvent.from_dict(payload) == evt


def test_paint_grid_pin_round_trips_negative_coordinates():
    evt = PaintGridPinEvent(x=-1920, y=-40)
    assert PaintGridPinEvent.from_dict(evt.to_dict()) == evt


def test_paint_grid_pin_round_trips_the_origin():
    evt = PaintGridPinEvent(x=0, y=0)
    assert PaintGridPinEvent.from_dict(evt.to_dict()) == evt


def test_paint_grid_pin_action_name():
    assert PAINT_GRID_PIN_ACTION == "paint_grid_pin"


def test_paint_grid_pin_not_a_mapping_raises():
    with pytest.raises(PaintGridPinEventSchemaError):
        PaintGridPinEvent.from_dict(["x", "y"])


def test_paint_grid_pin_missing_action_raises():
    with pytest.raises(PaintGridPinEventSchemaError):
        PaintGridPinEvent.from_dict({"x": 1, "y": 2})


def test_paint_grid_pin_wrong_action_raises():
    with pytest.raises(PaintGridPinEventSchemaError):
        PaintGridPinEvent.from_dict(
            {"action": "paint_grid", "x": 1, "y": 2}
        )


@pytest.mark.parametrize("field", ["x", "y"])
def test_paint_grid_pin_missing_field_raises(field):
    payload = PaintGridPinEvent(x=1, y=2).to_dict()
    del payload[field]
    with pytest.raises(PaintGridPinEventSchemaError):
        PaintGridPinEvent.from_dict(payload)


@pytest.mark.parametrize("bad", ["1", 1.5, None, True])
def test_paint_grid_pin_non_int_coordinate_raises(bad):
    payload = PaintGridPinEvent(x=1, y=2).to_dict()
    payload["x"] = bad
    with pytest.raises(PaintGridPinEventSchemaError):
        PaintGridPinEvent.from_dict(payload)


# ===========================================================================
# ClearGridEvent
# ===========================================================================


def test_clear_grid_round_trip():
    evt = ClearGridEvent()
    payload = evt.to_dict()
    assert payload == {"action": CLEAR_GRID_ACTION}
    assert ClearGridEvent.from_dict(payload) == evt


def test_clear_grid_action_name():
    assert CLEAR_GRID_ACTION == "clear_grid"


def test_clear_grid_not_a_mapping_raises():
    with pytest.raises(ClearGridEventSchemaError):
        ClearGridEvent.from_dict(None)


def test_clear_grid_missing_action_raises():
    with pytest.raises(ClearGridEventSchemaError):
        ClearGridEvent.from_dict({})


def test_clear_grid_wrong_action_raises():
    with pytest.raises(ClearGridEventSchemaError):
        ClearGridEvent.from_dict({"action": "clear_overlay"})


def test_clear_grid_tolerates_extra_keys():
    """Forward compatibility: an unknown key does not sink the clear.

    A clear that gets dropped leaves the grid painted on screen with
    nothing driving it, which is the worst failure this schema has.
    """

    assert ClearGridEvent.from_dict(
        {"action": CLEAR_GRID_ACTION, "future_key": 1}
    ) == ClearGridEvent()


# ---------------------------------------------------------------------------
# PaintGridPinEvent producer-side validation (wh-mouse-grid.1.25)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["x", "y"])
def test_paint_grid_pin_to_dict_rejects_a_non_int_coordinate(field):
    """wh-mouse-grid.1.25: the pin producer validates its coordinates too.

    The remaining sibling of wh-mouse-grid.1.23: PaintGridEvent validates
    producer coordinates, but the pin event emitted x/y unchecked -- a bad
    mark point serialized here, the GUI's safe_parse dropped it, and the
    paint manager kept displaying the PREVIOUS pin while Logic retained the
    new mark, so a later "drag" starts from a point the user never saw.
    Producer and consumer must validate the same fields.
    """

    evt = dataclasses.replace(
        PaintGridPinEvent(x=100, y=200), **{field: "bad-coordinate"}
    )
    with pytest.raises(PaintGridPinEventSchemaError):
        evt.to_dict()


@pytest.mark.parametrize("field", ["x", "y"])
def test_paint_grid_pin_to_dict_rejects_a_bool_coordinate(field):
    """bool is a subclass of int; the pin producer must not emit one."""

    evt = dataclasses.replace(PaintGridPinEvent(x=100, y=200), **{field: True})
    with pytest.raises(PaintGridPinEventSchemaError):
        evt.to_dict()
