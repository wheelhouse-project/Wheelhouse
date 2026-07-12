"""Input-side tests for the refresh_overlay_snapshot handler
(wh-overlay-snapshot-keepalive).

``refresh_overlay_snapshot`` is the Input side of the Logic 15-second overlay
keepalive. Logic sends it every keepalive tick for the snapshot the overlay is
currently showing; the handler slides that snapshot's TTL anchor via
``ElementFinder.refresh_snapshot_ttl`` so a numbered overlay left on screen past
the TTL stays clickable. Logic does NOT block on the ack, but the handler emits
exactly one ``PinSnapshotResponse`` (reused as the small Schema-A ack) so the
awaiting Future resolves and the demuxer does not leak.

Unlike ``pin_snapshot`` there is NO stale-generation watermark: a refresh
carries no generation, and refreshing a superseded snapshot's TTL briefly is
harmless (it is unpinned and aged out normally). The ``pinned`` field of the
reused response echoes whether the store found and refreshed the snapshot.

Covered here:
  * a known snapshot -> refresh_snapshot_ttl called, status=ok, pinned=True.
  * an unknown / already-expired snapshot -> pinned=False with a reason.
  * overlay-disabled-by-config short-circuit -> pinned=False,
    reason=disabled_by_config, the store is never touched.
  * a non-str snapshot_id -> status=error, reason=invalid_request, store
    untouched.
  * never-raise: an unexpected internal error maps to status=error.
  * exactly one response per request_id on every path; request_id + action
    attached.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

_MOD = "ui.ui_action_handler"


def _make_handler(config=None):
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):

        from ui.ui_action_handler import UIActionHandler

        q = MagicMock()
        return UIActionHandler(
            response_queue=q,
            config=config or {"ui_actions": {}},
        )


def _wire_finder(handler, *, refresh_result: bool = True) -> MagicMock:
    from ui.click_config import ClickConfig

    finder = MagicMock()
    finder.refresh_snapshot_ttl.return_value = refresh_result
    handler._click_element_finder = finder
    handler._click_config = ClickConfig.from_raw({})
    return finder


def _last_response(handler, *, request_id: str) -> PinSnapshotResponse:
    assert handler.response_queue.put.call_count == 1
    payload = handler.response_queue.put.call_args[0][0]
    assert payload["action"] == "refresh_overlay_snapshot"
    assert payload["request_id"] == request_id
    return PinSnapshotResponse.from_dict(payload)


def test_refresh_known_snapshot_slides_ttl_and_acks_ok():
    handler = _make_handler()
    finder = _wire_finder(handler, refresh_result=True)

    handler.refresh_overlay_snapshot(
        overlay_session_id=5, snapshot_id="walk-1", request_id="req-r-1",
    )

    finder.refresh_snapshot_ttl.assert_called_once_with("walk-1")
    resp = _last_response(handler, request_id="req-r-1")
    assert resp.status == "ok"
    assert resp.pinned is True
    assert resp.reason is None
    assert resp.overlay_session_id == 5
    assert resp.snapshot_id == "walk-1"


def test_refresh_unknown_snapshot_acks_not_refreshed():
    handler = _make_handler()
    finder = _wire_finder(handler, refresh_result=False)

    handler.refresh_overlay_snapshot(
        overlay_session_id=5, snapshot_id="walk-gone", request_id="req-r-2",
    )

    finder.refresh_snapshot_ttl.assert_called_once_with("walk-gone")
    resp = _last_response(handler, request_id="req-r-2")
    assert resp.status == "ok"
    assert resp.pinned is False
    assert resp.reason == "unknown_snapshot"


def test_refresh_disabled_overlay_short_circuits():
    handler = _make_handler(
        config={"ui_actions": {}, "click": {"overlay_enabled": False}},
    )
    # No finder injected; the gate returns None and the store is never built.
    handler.refresh_overlay_snapshot(
        overlay_session_id=1, snapshot_id="walk-1", request_id="req-r-3",
    )
    resp = _last_response(handler, request_id="req-r-3")
    assert resp.status == "ok"
    assert resp.pinned is False
    assert resp.reason == "disabled_by_config"
    assert resp.snapshot_id == "walk-1"


def test_refresh_non_str_snapshot_id_errors_without_touching_store():
    handler = _make_handler()
    finder = _wire_finder(handler)

    handler.refresh_overlay_snapshot(
        overlay_session_id=5, snapshot_id=123, request_id="req-r-4",
    )

    finder.refresh_snapshot_ttl.assert_not_called()
    resp = _last_response(handler, request_id="req-r-4")
    assert resp.status == "error"
    assert resp.reason == "invalid_request"


def test_refresh_unexpected_error_maps_to_status_error():
    handler = _make_handler()
    finder = _wire_finder(handler)
    finder.refresh_snapshot_ttl.side_effect = RuntimeError("kaboom")

    handler.refresh_overlay_snapshot(
        overlay_session_id=2, snapshot_id="walk-1", request_id="req-r-5",
    )

    resp = _last_response(handler, request_id="req-r-5")
    assert resp.status == "error"
    assert resp.pinned is False
    assert resp.reason is not None


def test_refresh_never_raises_when_response_queue_put_fails():
    handler = _make_handler()
    _wire_finder(handler)
    handler.response_queue.put.side_effect = RuntimeError("queue dead")
    # Must not raise even though the enqueue fails.
    handler.refresh_overlay_snapshot(
        overlay_session_id=5, snapshot_id="walk-1", request_id="req-r-6",
    )
