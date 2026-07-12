"""Hostile-Mapping guard tests for the IPC schema from_dict family.

wh-schema-hostile-mapping-guard (from codex finding wh-n29v.81.1). The
wh-uf54 graceful-degrade boundary promises each schema's from_dict lets
ONLY its typed SchemaError escape. A Mapping subclass whose __contains__
or __getitem__ raises passes the isinstance gate but would otherwise
bubble the RAW exception out of the ``key in payload`` / ``payload[key]``
accesses inside the parse body. ShowNumberedOverlayResponse got the
try/except wrapper in wh-n29v.81.1; these tests hold the REST of the
family to the same standard.

Reachability is low (the real transport is pickle, which reconstructs a
plain dict), so this is consistency hardening, not a live-bug fix.
"""
from collections.abc import Mapping

import pytest

from services.wheelhouse.shared.click_element import (
    ClickElementResponse,
    ClickElementResponseSchemaError,
)
from services.wheelhouse.shared.click_notice import (
    ClickNoticeEvent,
    ClickNoticeSchemaError,
)
from services.wheelhouse.shared.clear_overlay import (
    ClearOverlayEvent,
    ClearOverlayEventSchemaError,
)
from services.wheelhouse.shared.overlay_state_changed import (
    OverlayStateChangedEvent,
    OverlayStateChangedEventSchemaError,
)
from services.wheelhouse.shared.paint_overlay import (
    PaintOverlayEvent,
    PaintOverlayEventSchemaError,
)
from services.wheelhouse.shared.pin_snapshot import (
    PinSnapshotResponse,
    PinSnapshotResponseSchemaError,
)
from services.wheelhouse.shared.show_numbered_overlay import (
    ShowNumberedOverlayResponse,
    ShowNumberedOverlayResponseSchemaError,
)
from services.wheelhouse.shared.snapshot_item_clicked import (
    SnapshotItemClickedEvent,
    SnapshotItemClickedSchemaError,
)
from services.wheelhouse.shared.start_overlay_walk import (
    StartOverlayWalkResponse,
    StartOverlayWalkResponseSchemaError,
)


class _HostileContains(Mapping):
    """Mapping whose __contains__ raises AttributeError.

    ``key in payload`` is usually the first mapping access a from_dict
    makes, so this simulates the earliest possible raw-exception leak.
    """

    def __contains__(self, key):
        raise AttributeError("hostile __contains__")

    def __getitem__(self, key):
        raise AttributeError("hostile __getitem__")

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


class _HostileGetitem(Mapping):
    """Mapping whose __contains__ succeeds but __getitem__ raises TypeError.

    Catches parsers that check ``key in payload`` and then trust
    ``payload[key]``.
    """

    def __contains__(self, key):
        return True

    def __getitem__(self, key):
        raise TypeError("hostile __getitem__")

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


FAMILY = [
    pytest.param(ClickElementResponse, ClickElementResponseSchemaError,
                 id="click_element"),
    pytest.param(ClickNoticeEvent, ClickNoticeSchemaError, id="click_notice"),
    pytest.param(ClearOverlayEvent, ClearOverlayEventSchemaError,
                 id="clear_overlay"),
    pytest.param(OverlayStateChangedEvent, OverlayStateChangedEventSchemaError,
                 id="overlay_state_changed"),
    pytest.param(PaintOverlayEvent, PaintOverlayEventSchemaError,
                 id="paint_overlay"),
    pytest.param(PinSnapshotResponse, PinSnapshotResponseSchemaError,
                 id="pin_snapshot"),
    pytest.param(ShowNumberedOverlayResponse,
                 ShowNumberedOverlayResponseSchemaError,
                 id="show_numbered_overlay"),
    pytest.param(SnapshotItemClickedEvent, SnapshotItemClickedSchemaError,
                 id="snapshot_item_clicked"),
    pytest.param(StartOverlayWalkResponse, StartOverlayWalkResponseSchemaError,
                 id="start_overlay_walk"),
]


@pytest.mark.parametrize("schema_cls,error_cls", FAMILY)
def test_hostile_contains_raises_typed_error(schema_cls, error_cls):
    with pytest.raises(error_cls):
        schema_cls.from_dict(_HostileContains())


@pytest.mark.parametrize("schema_cls,error_cls", FAMILY)
def test_hostile_getitem_raises_typed_error(schema_cls, error_cls):
    with pytest.raises(error_cls):
        schema_cls.from_dict(_HostileGetitem())
