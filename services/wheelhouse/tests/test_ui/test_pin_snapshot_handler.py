"""End-to-end Input-side tests for the pin_snapshot / unpin_snapshot handlers
(wh-n29v.41).

``pin_snapshot`` and ``unpin_snapshot`` are the active-overlay pin transport:
Logic owns the pin and drives the multi-snapshot store's
``ElementFinder.pin`` / ``ElementFinder.unpin`` via these two Input-side
handlers. Each emits exactly one ``PinSnapshotResponse`` so the Logic-side
awaiting Future resolves (Logic does NOT block the paint on the ack, but the
demuxer Future must still resolve).

``pin_snapshot`` carries ``(overlay_session_id, snapshot_id, paint_generation)``
and rejects a stale ``(overlay_session_id, paint_generation)``.
``unpin_snapshot`` carries ``(overlay_session_id, snapshot_id)`` only and clears
by identity (no generation check).

Stale rejection mechanism (the design point, r1c.2 / r1c.1): the store keys
snapshots by ``snapshot_id`` ONLY and tracks no generation; Logic owns the
authoritative generation comparison and drops a superseded WALK response before
it would ever dispatch a pin. As Input-side defence-in-depth, the handler keeps
a SINGLE bounded watermark ``(latest_session_id, latest_accepted_generation)``
(not a per-session dict -- wh-n29v.42.1). ``overlay_session_id`` is monotonic and
the overlay state machine is single, so a pin is rejected when its
``overlay_session_id`` is OLDER than the latest seen, OR (within the latest
session) its ``paint_generation`` is STRICTLY OLDER than the latest accepted one.
Equal-or-newer generations and any newer session are accepted. The watermark
advances on any accepted dispatch BEFORE the store pin, so a failed pin still
advances "latest seen" (wh-n29v.42.2). ``unpin_snapshot`` is clear-by-identity
and never consults the watermark.

Covered here:
  * pin a known snapshot_id with a current generation -> store.pin called,
    status=ok, pinned=True, echoing overlay_session_id + snapshot_id.
  * pin an unknown snapshot_id -> pinned=False with a reason; store.pin
    consulted (returns False).
  * pin with a stale (overlay_session_id, paint_generation) -> rejected:
    pinned=False, reason names staleness, store.pin NOT called.
  * unpin clears by snapshot_id (no generation) -> store.unpin called,
    pinned=False ack.
  * overlay-disabled-by-config short-circuit -> pinned=False,
    reason=disabled_by_config, store never touched, exactly one response.
  * never-raise: an unexpected internal error maps to status=error with
    exactly one response.
  * exactly one response per request_id on every path; request_id + action
    attached.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

_MOD = "ui.ui_action_handler"


@pytest.fixture
def handler():
    """Build a UIActionHandler with specialist components mocked.

    [click] (and the overlay) defaults to enabled via ClickConfig.from_raw on
    an empty block, so a mock finder injected at self._click_element_finder is
    reachable through _get_overlay_walk_finder once _click_config is set.
    """
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
        h = UIActionHandler(response_queue=q, config={"ui_actions": {}})
        yield h


def _wire_finder(handler, *, pin_result: bool = True,
                 unpin_result: bool = True) -> MagicMock:
    """Inject a mock finder + a valid (enabled) ClickConfig so the overlay
    finder resolves to the mock.
    """
    from ui.click_config import ClickConfig

    finder = MagicMock()
    finder.pin.return_value = pin_result
    finder.unpin.return_value = unpin_result
    handler._click_element_finder = finder
    handler._click_config = ClickConfig.from_raw({})
    return finder


def _last_response(handler, *, action: str,
                   request_id: str) -> PinSnapshotResponse:
    """Assert exactly one response was enqueued and parse it."""
    assert handler.response_queue.put.call_count == 1
    payload = handler.response_queue.put.call_args[0][0]
    assert payload["action"] == action
    assert payload["request_id"] == request_id
    return PinSnapshotResponse.from_dict(payload)


# ---------------------------------------------------------------------------
# pin_snapshot: happy path.
# ---------------------------------------------------------------------------


def test_pin_known_snapshot_pins_and_acks_ok(handler):
    finder = _wire_finder(handler, pin_result=True)

    handler.pin_snapshot(
        overlay_session_id=5,
        snapshot_id="walk-1",
        paint_generation=2,
        request_id="req-pin-1",
    )

    finder.pin.assert_called_once_with("walk-1")
    resp = _last_response(handler, action="pin_snapshot",
                          request_id="req-pin-1")
    assert resp.status == "ok"
    assert resp.pinned is True
    assert resp.overlay_session_id == 5
    assert resp.snapshot_id == "walk-1"
    assert resp.reason is None


def test_pin_unknown_snapshot_acks_pinned_false_with_reason(handler):
    finder = _wire_finder(handler, pin_result=False)

    handler.pin_snapshot(
        overlay_session_id=5,
        snapshot_id="walk-gone",
        paint_generation=0,
        request_id="req-pin-1",
    )

    finder.pin.assert_called_once_with("walk-gone")
    resp = _last_response(handler, action="pin_snapshot",
                          request_id="req-pin-1")
    assert resp.status == "ok"
    assert resp.pinned is False
    assert resp.reason is not None
    assert resp.snapshot_id == "walk-gone"


# ---------------------------------------------------------------------------
# pin_snapshot: stale (overlay_session_id, paint_generation) rejection.
# ---------------------------------------------------------------------------


def test_pin_stale_generation_is_rejected_without_pinning(handler):
    finder = _wire_finder(handler, pin_result=True)

    # gen 3 accepted first (establishes the latest accepted generation = 3).
    handler.pin_snapshot(
        overlay_session_id=5,
        snapshot_id="walk-3",
        paint_generation=3,
        request_id="req-pin-newer",
    )
    assert finder.pin.call_count == 1
    handler.response_queue.put.reset_mock()
    finder.pin.reset_mock()

    # gen 1 arrives late for the SAME session -> stale, rejected.
    handler.pin_snapshot(
        overlay_session_id=5,
        snapshot_id="walk-1",
        paint_generation=1,
        request_id="req-pin-stale",
    )

    # The store was NOT touched for the stale pin.
    finder.pin.assert_not_called()
    resp = _last_response(handler, action="pin_snapshot",
                          request_id="req-pin-stale")
    assert resp.status == "ok"
    assert resp.pinned is False
    # Exact reason tag, not a substring (wh-n29v.44.5): a same-session
    # strictly-older generation is stale_generation, distinct from the older-
    # session stale_session tag. The substring "stale" matched both, so a
    # refactor that swapped the two tags would not be caught.
    assert resp.reason == "stale_generation"
    # Identity still echoed.
    assert resp.overlay_session_id == 5
    assert resp.snapshot_id == "walk-1"


def test_pin_equal_generation_is_accepted(handler):
    finder = _wire_finder(handler, pin_result=True)

    handler.pin_snapshot(
        overlay_session_id=5, snapshot_id="walk-2", paint_generation=2,
        request_id="r1",
    )
    handler.response_queue.put.reset_mock()
    finder.pin.reset_mock()

    # Same generation again (e.g. a re-dispatch of the SAME paint) is NOT stale.
    handler.pin_snapshot(
        overlay_session_id=5, snapshot_id="walk-2", paint_generation=2,
        request_id="r2",
    )
    finder.pin.assert_called_once_with("walk-2")
    resp = _last_response(handler, action="pin_snapshot", request_id="r2")
    assert resp.pinned is True


def test_pin_newer_session_is_accepted(handler):
    finder = _wire_finder(handler, pin_result=True)

    # Session 5 reaches gen 9.
    handler.pin_snapshot(
        overlay_session_id=5, snapshot_id="a", paint_generation=9,
        request_id="r1",
    )
    handler.response_queue.put.reset_mock()
    finder.pin.reset_mock()

    # Session 6 at gen 0 is NOT stale -- a strictly-newer overlay session
    # supersedes the prior one and resets the per-session generation watermark.
    handler.pin_snapshot(
        overlay_session_id=6, snapshot_id="b", paint_generation=0,
        request_id="r2",
    )
    finder.pin.assert_called_once_with("b")
    resp = _last_response(handler, action="pin_snapshot", request_id="r2")
    assert resp.pinned is True


# ---------------------------------------------------------------------------
# pin_snapshot: single-pair watermark -- older overlay session rejected, and
# the tracking state is O(1) (one (session_id, generation) pair, NOT a dict
# that grows one entry per overlay session). wh-n29v.42.1.
# ---------------------------------------------------------------------------


def test_pin_older_session_is_rejected_as_stale(handler):
    # overlay_session_id is monotonic (allocated when the overlay state machine
    # leaves `closed`) and the state machine is single, so a pin carrying an
    # overlay session id OLDER than the latest seen is from a superseded
    # session that Logic has already torn down. It must be rejected without
    # touching the store, just like a stale generation within the current
    # session (design v4 r1c.2).
    finder = _wire_finder(handler, pin_result=True)

    # Session 6 establishes the latest seen session.
    handler.pin_snapshot(
        overlay_session_id=6, snapshot_id="newer", paint_generation=0,
        request_id="r1",
    )
    assert finder.pin.call_count == 1
    handler.response_queue.put.reset_mock()
    finder.pin.reset_mock()

    # A pin for an OLDER session id is stale, even with a high generation
    # (the generation namespace is per-session; the session itself is stale).
    handler.pin_snapshot(
        overlay_session_id=5, snapshot_id="older", paint_generation=99,
        request_id="r2",
    )

    finder.pin.assert_not_called()
    resp = _last_response(handler, action="pin_snapshot", request_id="r2")
    assert resp.status == "ok"
    assert resp.pinned is False
    # Exact reason tag, not a substring (wh-n29v.44.5): an older overlay
    # session is stale_session, distinct from the same-session stale_generation
    # tag.
    assert resp.reason == "stale_session"
    assert resp.overlay_session_id == 5
    assert resp.snapshot_id == "older"


def test_watermark_state_is_bounded_across_many_overlay_sessions(handler):
    # The per-session generation state must be a single (session_id,
    # generation) pair, NOT a dict that adds one permanent entry per overlay
    # session for the lifetime of the long-lived Input process. Drive many
    # monotonically-increasing overlay sessions and assert the tracking state
    # stays O(1).
    finder = _wire_finder(handler, pin_result=True)

    for sid in range(1, 101):
        handler.pin_snapshot(
            overlay_session_id=sid, snapshot_id=f"s{sid}", paint_generation=0,
            request_id=f"r{sid}",
        )

    # Only the latest session's watermark is retained -- a single pair.
    assert handler._latest_pin_watermark == (100, 0)
    # The watermark must be a single bounded (session_id, generation) pair, NOT
    # a dict that adds one entry per overlay session (wh-n29v.44.3). The earlier
    # assertion read getattr(handler, "_latest_pin_generation", None) -- a name
    # that was renamed to _latest_pin_watermark in wh-n29v.42.1, so it always
    # returned None and the check was vacuous. Assert the REAL attribute is a
    # 2-tuple, so a future per-session-dict regression fails here.
    assert isinstance(handler._latest_pin_watermark, tuple)
    assert not isinstance(handler._latest_pin_watermark, dict)
    assert len(handler._latest_pin_watermark) == 2


# ---------------------------------------------------------------------------
# pin_snapshot: the stale-generation watermark must advance on any accepted
# (non-stale) dispatch BEFORE the store pin, so a FAILED newer pin (snapshot
# already TTL-evicted) still advances "latest seen" and a later strictly-older
# generation is still rejected. wh-n29v.42.2.
# ---------------------------------------------------------------------------


def test_failed_newer_pin_still_rejects_later_stale_older(handler):
    finder = _wire_finder(handler)

    # gen 2 dispatch: the snapshot is already gone from the store (TTL-evicted
    # or never stored), so finder.pin returns False.
    finder.pin.return_value = False
    handler.pin_snapshot(
        overlay_session_id=5, snapshot_id="A", paint_generation=2,
        request_id="r1",
    )
    assert finder.pin.call_count == 1  # the pin WAS attempted
    handler.response_queue.put.reset_mock()
    finder.pin.reset_mock()

    # A delayed / re-dispatched strictly-older gen 1 for the SAME session
    # arrives. Even though gen 2's pin FAILED, the watermark advanced to gen 2,
    # so gen 1 must be rejected as stale WITHOUT touching the store.
    finder.pin.return_value = True  # B is still in the store -- must not matter
    handler.pin_snapshot(
        overlay_session_id=5, snapshot_id="B", paint_generation=1,
        request_id="r2",
    )

    finder.pin.assert_not_called()
    resp = _last_response(handler, action="pin_snapshot", request_id="r2")
    assert resp.status == "ok"
    assert resp.pinned is False
    # Exact reason tag, not a substring (wh-n29v.44.5): gen 1 < gen 2 in the
    # same session is stale_generation.
    assert resp.reason == "stale_generation"


# ---------------------------------------------------------------------------
# pin_snapshot: malformed-IPC field validation (wh-n29v.43.1). Malformed input
# (wrong types from a Logic bug or a corrupted message) must be rejected BEFORE
# the store or the watermark is touched. Otherwise a bad value written into the
# single-pair watermark makes every later VALID pin raise on the comparison
# (e.g. 5 < "bad") and return status=error until the Input process restarts.
# ---------------------------------------------------------------------------


def test_pin_non_int_session_id_rejected_without_touching_store(handler):
    finder = _wire_finder(handler, pin_result=True)

    handler.pin_snapshot(
        overlay_session_id="bad",  # type: ignore[arg-type]
        snapshot_id="walk-1",
        paint_generation=0,
        request_id="req-bad",
    )

    finder.pin.assert_not_called()
    resp = _last_response(handler, action="pin_snapshot", request_id="req-bad")
    assert resp.status == "error"
    assert resp.pinned is False
    assert resp.reason == "invalid_request"
    # The watermark was NOT mutated by the malformed request.
    assert getattr(handler, "_latest_pin_watermark", None) is None


def test_pin_non_int_generation_rejected_without_touching_store(handler):
    finder = _wire_finder(handler, pin_result=True)

    handler.pin_snapshot(
        overlay_session_id=5,
        snapshot_id="walk-1",
        paint_generation="bad",  # type: ignore[arg-type]
        request_id="req-bad",
    )

    finder.pin.assert_not_called()
    resp = _last_response(handler, action="pin_snapshot", request_id="req-bad")
    assert resp.status == "error"
    assert resp.pinned is False
    assert resp.reason == "invalid_request"
    assert getattr(handler, "_latest_pin_watermark", None) is None


def test_pin_bool_session_id_rejected(handler):
    # bool is a subclass of int; a True/False overlay_session_id is malformed
    # (overlay_session_id is a real monotonic count) and must be rejected.
    finder = _wire_finder(handler, pin_result=True)

    handler.pin_snapshot(
        overlay_session_id=True,  # type: ignore[arg-type]
        snapshot_id="walk-1",
        paint_generation=0,
        request_id="req-bad",
    )

    finder.pin.assert_not_called()
    resp = _last_response(handler, action="pin_snapshot", request_id="req-bad")
    assert resp.status == "error"
    assert resp.reason == "invalid_request"


def test_pin_non_str_snapshot_id_rejected(handler):
    finder = _wire_finder(handler, pin_result=True)

    handler.pin_snapshot(
        overlay_session_id=5,
        snapshot_id=123,  # type: ignore[arg-type]
        paint_generation=0,
        request_id="req-bad",
    )

    finder.pin.assert_not_called()
    resp = _last_response(handler, action="pin_snapshot", request_id="req-bad")
    assert resp.status == "error"
    assert resp.reason == "invalid_request"


def test_malformed_pin_does_not_poison_later_valid_pins(handler):
    # The core regression: a malformed pin must not write a bad value into the
    # watermark, because a later VALID pin would then compare against it
    # (5 < "bad") and raise, returning status=error forever.
    finder = _wire_finder(handler, pin_result=True)

    # Malformed pin first (no watermark yet -- the dangerous case).
    handler.pin_snapshot(
        overlay_session_id="bad",  # type: ignore[arg-type]
        snapshot_id="walk-bad",
        paint_generation=0,
        request_id="r-bad",
    )
    handler.response_queue.put.reset_mock()
    finder.pin.reset_mock()

    # A subsequent VALID pin must still succeed.
    handler.pin_snapshot(
        overlay_session_id=5,
        snapshot_id="walk-good",
        paint_generation=2,
        request_id="r-good",
    )

    finder.pin.assert_called_once_with("walk-good")
    resp = _last_response(handler, action="pin_snapshot", request_id="r-good")
    assert resp.status == "ok"
    assert resp.pinned is True


# ---------------------------------------------------------------------------
# unpin_snapshot: clear by identity, no generation check.
# ---------------------------------------------------------------------------


def test_unpin_clears_by_snapshot_id(handler):
    finder = _wire_finder(handler, unpin_result=True)

    handler.unpin_snapshot(
        overlay_session_id=5,
        snapshot_id="walk-1",
        request_id="req-unpin-1",
    )

    finder.unpin.assert_called_once_with("walk-1")
    resp = _last_response(handler, action="unpin_snapshot",
                          request_id="req-unpin-1")
    assert resp.status == "ok"
    assert resp.pinned is False
    assert resp.overlay_session_id == 5
    assert resp.snapshot_id == "walk-1"


def test_unpin_unknown_snapshot_still_acks_pinned_false(handler):
    finder = _wire_finder(handler, unpin_result=False)

    handler.unpin_snapshot(
        overlay_session_id=5,
        snapshot_id="walk-gone",
        request_id="req-unpin-1",
    )

    finder.unpin.assert_called_once_with("walk-gone")
    resp = _last_response(handler, action="unpin_snapshot",
                          request_id="req-unpin-1")
    assert resp.status == "ok"
    assert resp.pinned is False


def test_unpin_does_not_consult_generation(handler):
    # Even after a high accepted generation on a session, an unpin with no
    # generation field must clear by identity (clear-by-identity is always
    # safe -- it only relaxes LRU immunity).
    finder = _wire_finder(handler, pin_result=True, unpin_result=True)
    handler.pin_snapshot(
        overlay_session_id=5, snapshot_id="walk-9", paint_generation=9,
        request_id="r1",
    )
    handler.response_queue.put.reset_mock()

    handler.unpin_snapshot(
        overlay_session_id=5, snapshot_id="walk-1", request_id="r2",
    )
    finder.unpin.assert_called_once_with("walk-1")
    resp = _last_response(handler, action="unpin_snapshot", request_id="r2")
    assert resp.pinned is False


# ---------------------------------------------------------------------------
# unpin_snapshot: malformed-IPC field validation (wh-n29v.44.1). unpin has no
# watermark to poison, but a malformed (overlay_session_id, snapshot_id) echo
# would make the Logic-side PinSnapshotResponse.from_dict raise instead of
# resolving the awaiting Future. So unpin mirrors pin: validate the field types
# before touching the store, reject malformed input with
# status="error" reason="invalid_request", and coerce the echoed identity to
# schema-safe primitives so the response always parses.
# ---------------------------------------------------------------------------


def test_unpin_non_int_session_id_rejected_without_touching_store(handler):
    finder = _wire_finder(handler, unpin_result=True)

    handler.unpin_snapshot(
        overlay_session_id="bad",  # type: ignore[arg-type]
        snapshot_id="walk-1",
        request_id="req-bad",
    )

    finder.unpin.assert_not_called()
    resp = _last_response(handler, action="unpin_snapshot", request_id="req-bad")
    assert resp.status == "error"
    assert resp.pinned is False
    assert resp.reason == "invalid_request"


def test_unpin_bool_session_id_rejected(handler):
    # bool is a subclass of int; a True/False overlay_session_id is malformed.
    finder = _wire_finder(handler, unpin_result=True)

    handler.unpin_snapshot(
        overlay_session_id=True,  # type: ignore[arg-type]
        snapshot_id="walk-1",
        request_id="req-bad",
    )

    finder.unpin.assert_not_called()
    resp = _last_response(handler, action="unpin_snapshot", request_id="req-bad")
    assert resp.status == "error"
    assert resp.reason == "invalid_request"


def test_unpin_non_str_snapshot_id_rejected(handler):
    finder = _wire_finder(handler, unpin_result=True)

    handler.unpin_snapshot(
        overlay_session_id=5,
        snapshot_id=123,  # type: ignore[arg-type]
        request_id="req-bad",
    )

    finder.unpin.assert_not_called()
    resp = _last_response(handler, action="unpin_snapshot", request_id="req-bad")
    assert resp.status == "error"
    assert resp.reason == "invalid_request"


def test_unpin_malformed_echo_is_schema_safe(handler):
    # Even when rejecting malformed input, the echoed identity must be coerced
    # to schema-safe primitives so the Logic-side from_dict parses the response
    # and resolves the awaiting Future (mirrors pin_snapshot's _emit coercion).
    finder = _wire_finder(handler, unpin_result=True)

    handler.unpin_snapshot(
        overlay_session_id="bad",  # type: ignore[arg-type]
        snapshot_id=123,  # type: ignore[arg-type]
        request_id="req-bad",
    )

    finder.unpin.assert_not_called()
    # _last_response calls PinSnapshotResponse.from_dict, which raises if the
    # echoed overlay_session_id / snapshot_id are not schema-safe primitives.
    resp = _last_response(handler, action="unpin_snapshot", request_id="req-bad")
    assert resp.status == "error"
    assert resp.reason == "invalid_request"
    assert resp.overlay_session_id == 0
    assert resp.snapshot_id == ""


# ---------------------------------------------------------------------------
# Disabled-overlay-config short-circuit.
# ---------------------------------------------------------------------------


def _disabled_handler():
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
        h = UIActionHandler(
            response_queue=q,
            config={"ui_actions": {}, "click": {"overlay_enabled": False}},
        )
        return h


def test_pin_disabled_overlay_short_circuits(handler):
    h = _disabled_handler()
    # No finder is injected; the gate returns None and the store is never built.
    h.pin_snapshot(
        overlay_session_id=1,
        snapshot_id="walk-1",
        paint_generation=0,
        request_id="req-pin-1",
    )
    resp = _last_response(h, action="pin_snapshot", request_id="req-pin-1")
    assert resp.status == "ok"
    assert resp.pinned is False
    assert resp.reason == "disabled_by_config"
    assert resp.overlay_session_id == 1
    assert resp.snapshot_id == "walk-1"


def test_unpin_disabled_overlay_short_circuits(handler):
    h = _disabled_handler()
    h.unpin_snapshot(
        overlay_session_id=1,
        snapshot_id="walk-1",
        request_id="req-unpin-1",
    )
    resp = _last_response(h, action="unpin_snapshot", request_id="req-unpin-1")
    assert resp.status == "ok"
    assert resp.pinned is False
    assert resp.reason == "disabled_by_config"


# ---------------------------------------------------------------------------
# Robustness: never raise; an unexpected error maps to status=error.
# ---------------------------------------------------------------------------


def test_pin_unexpected_error_maps_to_status_error(handler):
    from ui.click_config import ClickConfig

    boom_finder = MagicMock()
    boom_finder.pin.side_effect = RuntimeError("kaboom")
    handler._click_element_finder = boom_finder
    handler._click_config = ClickConfig.from_raw({})

    handler.pin_snapshot(
        overlay_session_id=2,
        snapshot_id="walk-1",
        paint_generation=0,
        request_id="req-pin-1",
    )

    resp = _last_response(handler, action="pin_snapshot",
                          request_id="req-pin-1")
    assert resp.status == "error"
    assert resp.pinned is False
    assert resp.reason is not None
    # Identity still echoed even on the crash path.
    assert resp.overlay_session_id == 2
    assert resp.snapshot_id == "walk-1"


def test_unpin_unexpected_error_maps_to_status_error(handler):
    from ui.click_config import ClickConfig

    boom_finder = MagicMock()
    boom_finder.unpin.side_effect = RuntimeError("kaboom")
    handler._click_element_finder = boom_finder
    handler._click_config = ClickConfig.from_raw({})

    handler.unpin_snapshot(
        overlay_session_id=2,
        snapshot_id="walk-1",
        request_id="req-unpin-1",
    )

    resp = _last_response(handler, action="unpin_snapshot",
                          request_id="req-unpin-1")
    assert resp.status == "error"
    assert resp.pinned is False
    assert resp.reason is not None


# ---------------------------------------------------------------------------
# Never-raise holds even when the response queue itself is dead (wh-n29v.44.4).
# This is the documented single exception to "exactly one response": when
# response_queue.put raises (a crashed Logic process or a closed queue), the
# handler logs and returns having emitted ZERO responses. It must NOT propagate
# the queue exception, and the orphaned Logic Future is covered by Logic's own
# timeout.
# ---------------------------------------------------------------------------


def test_pin_never_raises_when_response_queue_put_fails(handler):
    _wire_finder(handler, pin_result=True)
    handler.response_queue.put.side_effect = RuntimeError("queue dead")

    # Must not raise (pytest fails the test if it does).
    handler.pin_snapshot(
        overlay_session_id=1, snapshot_id="walk-1", paint_generation=0,
        request_id="req-pin-1",
    )
    # The handler attempted the single enqueue; the queue raised, so zero
    # responses landed -- the documented exception, not a contract violation.
    assert handler.response_queue.put.call_count == 1


def test_unpin_never_raises_when_response_queue_put_fails(handler):
    _wire_finder(handler, unpin_result=True)
    handler.response_queue.put.side_effect = RuntimeError("queue dead")

    # Must not raise.
    handler.unpin_snapshot(
        overlay_session_id=1, snapshot_id="walk-1", request_id="req-unpin-1",
    )
    assert handler.response_queue.put.call_count == 1


# ---------------------------------------------------------------------------
# Exactly one response per request_id on every path.
# ---------------------------------------------------------------------------


def test_pin_emits_exactly_one_response(handler):
    _wire_finder(handler, pin_result=True)
    handler.pin_snapshot(
        overlay_session_id=1, snapshot_id="walk-1", paint_generation=0,
        request_id="req-pin-1",
    )
    assert handler.response_queue.put.call_count == 1


def test_unpin_emits_exactly_one_response(handler):
    _wire_finder(handler, unpin_result=True)
    handler.unpin_snapshot(
        overlay_session_id=1, snapshot_id="walk-1", request_id="req-unpin-1",
    )
    assert handler.response_queue.put.call_count == 1


def test_handlers_in_handles_own_response_allowlist():
    from input_proc import _HANDLES_OWN_RESPONSE

    assert "pin_snapshot" in _HANDLES_OWN_RESPONSE
    assert "unpin_snapshot" in _HANDLES_OWN_RESPONSE
