"""Input-side tests for UIActionHandler.click_snapshot_item (wh-tab7j).

The numbered-overlay click handler is the Phase 1.5 sibling of click_element
(epic wh-l4h.1). When the user clicks a numbered overlay badge, Logic resolves
the display number to an ``item_id`` and forwards a ``click_snapshot_item``
request carrying ``snapshot_id`` + ``item_id`` (+ ``trace_id`` + ``request_id``).
The Input process:

  1. Validates the request fields.
  2. Gets the finder that holds the pinned snapshot store
     (``_get_overlay_walk_finder`` -- the SAME accessor show_numbered_overlay
     uses, so it hits the populated store and applies the overlay-enabled /
     automation-unavailable gates).
  3. Captures the current foreground identity.
  4. Looks up the pinned snapshot via ``finder.get_snapshot``.
  5. Finds the ElementMatch in ``snapshot.matches`` whose ``item_id`` matches.
  6. Runs ``ClickExecutor.click`` (the full pre-click verification block) and
     emits EXACTLY ONE ClickElementResponse with the same status/outcome
     pairing as click_element.

The handler is in ``_HANDLES_OWN_RESPONSE`` and must NEVER raise: every path
emits a response (ok or execution_failed) instead.

These tests drive the handler on a minimal stand-in (mirroring the stub-test
harness) injecting fake finder / snapshot / executor, so they stay headless
(no real COM, no real display).
"""

from __future__ import annotations

from typing import Any, Optional, cast
from unittest.mock import patch

import pytest

from services.wheelhouse.shared.click_element import ClickElementResponse
from tests.test_element_finder import _store_walk, make_multi_finder
from ui.element_types import ElementMatch, ElementQuery, WalkSnapshot
from ui.ui_action_handler import UIActionHandler

_MOD = "ui.ui_action_handler"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeQueue:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def put(self, item: dict) -> None:
        self.items.append(item)


class _FakeForeground:
    """Stand-in for the ForegroundContext _capture_click_foreground returns."""

    foreground_window = 1000
    foreground_pid = 4321
    foreground_process_name = "notepad.exe"
    foreground_window_creation_time = 99
    cursor_at_walk = (60, 45)
    cursor_monitor_id = 0


class _FakeClickResult:
    def __init__(
        self,
        outcome: str,
        reason: Optional[str],
        matched_name: Optional[str],
        clicked_via: Optional[str] = None,
    ) -> None:
        self.outcome = outcome
        self.reason = reason
        self.matched_name = matched_name
        self.clicked_via = clicked_via


class _FakeExecutor:
    """Captures the click() args and returns a preset ClickResult."""

    def __init__(self, result: _FakeClickResult) -> None:
        self._result = result
        self.calls: list[tuple[Any, Any, Any]] = []

    def click(self, winner: Any, snapshot_foreground: Any, query: Any):
        self.calls.append((winner, snapshot_foreground, query))
        return self._result


class _FakeFinder:
    """Returns a preset snapshot (or None) from get_snapshot, recording args.

    ``miss_cause`` is what ``describe_snapshot_miss`` returns (the Fix 3 cause
    string: ``ttl_expired`` / ``not_found`` / ``foreground_changed`` / None); it
    also records its calls so a test can assert the handler drives it with the
    same foreground identity it gave ``get_snapshot``.
    """

    def __init__(
        self, snapshot: Optional[WalkSnapshot], *, miss_cause: Any = None,
    ) -> None:
        self._snapshot = snapshot
        self._miss_cause = miss_cause
        self.get_snapshot_calls: list[dict] = []
        self.describe_calls: list[dict] = []

    def get_snapshot(self, snapshot_id: str, **kwargs):
        self.get_snapshot_calls.append({"snapshot_id": snapshot_id, **kwargs})
        return self._snapshot

    def describe_snapshot_miss(self, snapshot_id: str, **kwargs):
        self.describe_calls.append({"snapshot_id": snapshot_id, **kwargs})
        return self._miss_cause


def _match(item_id: str, name: str = "Cancel") -> ElementMatch:
    return ElementMatch(
        item_id=item_id,
        display_number=1,
        name=name,
        role="Button",
        bounds=(10, 20, 110, 70),
        monitor_id=0,
        score=1.0,
        is_eligible=True,
        source="primary",
        invoke_supported=True,
        is_enabled=True,
        control_ref=object(),
    )


def _snapshot(matches: list[ElementMatch], snapshot_id: str = "s1") -> WalkSnapshot:
    return WalkSnapshot(
        snapshot_id=snapshot_id,
        matches=matches,
        created_at_monotonic=0.0,
        foreground_window=1000,
        foreground_pid=4321,
        foreground_process_name="notepad.exe",
        foreground_window_creation_time=99,
        cursor_at_walk=(60, 45),
        cursor_monitor_id=0,
    )


class _Stub:
    """Minimal UIActionHandler stand-in.

    click_snapshot_item touches only ``self.response_queue``,
    ``self._get_overlay_walk_finder()``, ``self._get_click_executor()``, the
    module-level ``_capture_click_foreground``, and ``self._click_automation_root``
    (read on the finder-None branch). We provide overridable hooks for each so
    the handler can be driven without constructing the win32-heavy handler.
    """

    def __init__(
        self,
        *,
        finder: Optional[_FakeFinder],
        executor: Optional[_FakeExecutor] = None,
        automation_root: Any = None,
    ) -> None:
        self.response_queue = _FakeQueue()
        self._finder = finder
        self._executor = executor
        self._click_automation_root = automation_root

    def _get_overlay_walk_finder(self):
        return self._finder

    def _get_click_executor(self):
        return self._executor


def _call(stub: _Stub, **kwargs):
    """Invoke the real handler bound to the stub, with foreground patched."""
    with patch(f"{_MOD}._capture_click_foreground", return_value=_FakeForeground()):
        UIActionHandler.click_snapshot_item(cast(UIActionHandler, stub), **kwargs)


def _one_response(stub: _Stub) -> ClickElementResponse:
    assert len(stub.response_queue.items) == 1
    payload = stub.response_queue.items[0]
    assert payload["action"] == "click_snapshot_item"
    assert payload["request_id"] == "req-9"
    return ClickElementResponse.from_dict(payload)


# ---------------------------------------------------------------------------
# (a) Happy path
# ---------------------------------------------------------------------------


def test_happy_path_clicks_and_emits_ok():
    match = _match("uia-3", name="Cancel")
    finder = _FakeFinder(_snapshot([match]))
    executor = _FakeExecutor(
        _FakeClickResult("ok", None, "Cancel", clicked_via="invoke")
    )
    stub = _Stub(finder=finder, executor=executor)

    _call(stub, snapshot_id="s1", item_id="uia-3", request_id="req-9",
          trace_id="trace-ok")

    resp = _one_response(stub)
    assert resp.status == "ok"
    assert resp.outcome == "ok"
    assert resp.reason is None
    assert resp.matched_name == "Cancel"
    assert resp.matched_names == ("Cancel",)
    assert resp.snapshot_id == "s1"
    assert resp.snapshot_summary is None
    assert resp.trace_id == "trace-ok"
    # The located ElementMatch was handed to the executor (so the real
    # verification block runs), not re-walked.
    assert len(executor.calls) == 1
    assert executor.calls[0][0] is match
    # ClickExecutor.click(winner, snapshot_foreground, query): the query is a
    # minimal ElementQuery built from the match (consumed only by _coord_eligible).
    assert isinstance(executor.calls[0][2], ElementQuery)
    # get_snapshot was driven with the captured foreground identity.
    assert finder.get_snapshot_calls[0]["snapshot_id"] == "s1"
    assert finder.get_snapshot_calls[0]["current_foreground_window"] == 1000


# ---------------------------------------------------------------------------
# (b) Snapshot missing
# ---------------------------------------------------------------------------


def test_snapshot_missing_emits_snapshot_expired():
    finder = _FakeFinder(None)  # get_snapshot returns None
    stub = _Stub(finder=finder, executor=_FakeExecutor(
        _FakeClickResult("ok", None, "x")))

    _call(stub, snapshot_id="s1", item_id="uia-3", request_id="req-9")

    resp = _one_response(stub)
    assert resp.status == "error"
    assert resp.outcome == "execution_failed"
    assert resp.reason == "snapshot_expired"
    assert resp.snapshot_id == "s1"
    assert resp.snapshot_summary is None


def test_snapshot_missing_logs_specific_cause(caplog):
    """On a miss, the log names the exact cause (Fix 3) without changing the
    emitted reason tag.

    The bare ``snapshot_expired`` conflated three distinct causes -- a TTL
    expiry (trigger A), a never-stored / evicted id, and a foreground-identity
    change (trigger B). The handler now consults ``describe_snapshot_miss`` with
    the SAME foreground identity it gave ``get_snapshot`` and logs the cause, so
    a live report can tell the two triggers apart. The Schema-A reason tag stays
    ``snapshot_expired`` (routing / notice behaviour is unchanged).
    """
    import logging

    finder = _FakeFinder(None, miss_cause="foreground_changed")
    stub = _Stub(finder=finder, executor=_FakeExecutor(
        _FakeClickResult("ok", None, "x")))

    with caplog.at_level(logging.INFO, logger=_MOD):
        _call(stub, snapshot_id="s1", item_id="uia-3", request_id="req-9")

    # Reason tag unchanged.
    resp = _one_response(stub)
    assert resp.reason == "snapshot_expired"
    # describe_snapshot_miss was driven once with the same foreground identity
    # as get_snapshot (so the named cause matches the real miss).
    assert len(finder.describe_calls) == 1
    assert (finder.describe_calls[0]["current_foreground_window"]
            == finder.get_snapshot_calls[0]["current_foreground_window"])
    # The specific cause appears in the log.
    assert any("foreground_changed" in r.getMessage() for r in caplog.records)


class _OtherForeground:
    """A foreground identity that DIFFERS from the stored snapshot's window.

    Used to drive the real-finder foreground-mismatch (trigger B) miss.
    """

    foreground_window = 2000
    foreground_pid = 9999
    foreground_process_name = "other.exe"
    foreground_window_creation_time = 7
    cursor_at_walk = (0, 0)
    cursor_monitor_id = 0


def test_real_finder_ttl_miss_logs_ttl_expired_not_not_found(caplog):
    """Integration: a REAL ElementFinder TTL miss (trigger A) logs ttl_expired.

    This is the regression guard the _FakeFinder cause test cannot give. The
    real ``get_snapshot`` MUTATES on a miss -- it runs ``_sweep_ttl`` which drops
    the TTL-expired entry BEFORE returning None. So a ``describe_snapshot_miss``
    called AFTER ``get_snapshot`` sees an already-gone entry and reports
    ``not_found`` for what was really a TTL expiry, silently defeating the Fix 3
    log split for the exact trigger it targets
    (wh-overlay-snapshot-keepalive.1.1). The handler must call describe BEFORE
    get_snapshot. The plain _FakeFinder masks this because its get_snapshot does
    not mutate.
    """
    import logging

    now = {"t": 1000.0}
    finder = make_multi_finder(clock=lambda: now["t"], snapshot_ttl_seconds=30)
    sid = _store_walk(finder).snapshot.snapshot_id
    finder.pin(sid)  # the overlay pins it; pin blocks LRU, NOT TTL
    now["t"] += 31.0  # past the 30s TTL -> get_snapshot will sweep + drop it

    stub = _Stub(finder=finder, executor=_FakeExecutor(
        _FakeClickResult("ok", None, "x")))
    with caplog.at_level(logging.INFO, logger=_MOD):
        # The captured foreground (_FakeForeground) MATCHES the stored snapshot
        # (both 1000 / 4321 / notepad.exe / 99), so the ONLY miss cause is TTL.
        _call(stub, snapshot_id=sid, item_id="uia-anything", request_id="req-9")

    resp = _one_response(stub)
    assert resp.reason == "snapshot_expired"  # emitted tag unchanged
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "ttl_expired" in msgs
    assert "cause=not_found" not in msgs


def test_real_finder_foreground_miss_logs_foreground_changed_not_not_found(caplog):
    """Integration: a REAL ElementFinder foreground-identity miss (trigger B)
    logs foreground_changed, not not_found.

    The real ``get_snapshot`` calls ``_drop`` on the foreground-identity
    mismatch before returning None, so a describe call placed after it would see
    the entry already gone and mis-report ``not_found``. Companion to the TTL
    test above for the second trigger this epic targets.
    """
    import logging

    finder = make_multi_finder(snapshot_ttl_seconds=30)
    sid = _store_walk(finder).snapshot.snapshot_id  # foreground 1000/notepad/...
    finder.pin(sid)

    stub = _Stub(finder=finder, executor=_FakeExecutor(
        _FakeClickResult("ok", None, "x")))
    with caplog.at_level(logging.INFO, logger=_MOD):
        # Speak while a DIFFERENT window (2000) is foreground -> the entry is
        # present and within TTL, so the only miss cause is the foreground change.
        with patch(f"{_MOD}._capture_click_foreground",
                   return_value=_OtherForeground()):
            UIActionHandler.click_snapshot_item(
                cast(UIActionHandler, stub),
                snapshot_id=sid, item_id="uia-anything", request_id="req-9",
            )

    resp = _one_response(stub)
    assert resp.reason == "snapshot_expired"  # emitted tag unchanged
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "foreground_changed" in msgs
    assert "cause=not_found" not in msgs


# ---------------------------------------------------------------------------
# (c) item_id not in snapshot.matches
# ---------------------------------------------------------------------------


def test_item_id_not_found_emits_item_not_found():
    finder = _FakeFinder(_snapshot([_match("uia-1"), _match("uia-2")]))
    executor = _FakeExecutor(_FakeClickResult("ok", None, "x"))
    stub = _Stub(finder=finder, executor=executor)

    _call(stub, snapshot_id="s1", item_id="uia-99", request_id="req-9")

    resp = _one_response(stub)
    assert resp.status == "error"
    assert resp.outcome == "execution_failed"
    assert resp.reason == "item_not_found"
    assert resp.snapshot_id == "s1"
    # The executor never ran: there was nothing to click.
    assert executor.calls == []


# ---------------------------------------------------------------------------
# (d) Executor failure is passed through
# ---------------------------------------------------------------------------


def test_executor_failure_passes_reason_through():
    match = _match("uia-3", name="Cancel")
    finder = _FakeFinder(_snapshot([match]))
    executor = _FakeExecutor(
        _FakeClickResult("execution_failed", "bounds_stale", "Cancel")
    )
    stub = _Stub(finder=finder, executor=executor)

    _call(stub, snapshot_id="s1", item_id="uia-3", request_id="req-9")

    resp = _one_response(stub)
    assert resp.status == "error"
    assert resp.outcome == "execution_failed"
    assert resp.reason == "bounds_stale"
    assert resp.matched_name == "Cancel"
    assert resp.snapshot_id == "s1"
    assert len(executor.calls) == 1


def test_executor_failure_none_reason_falls_back_to_invoke_com_error():
    match = _match("uia-3")
    finder = _FakeFinder(_snapshot([match]))
    executor = _FakeExecutor(_FakeClickResult("execution_failed", None, "Cancel"))
    stub = _Stub(finder=finder, executor=executor)

    _call(stub, snapshot_id="s1", item_id="uia-3", request_id="req-9")

    resp = _one_response(stub)
    assert resp.outcome == "execution_failed"
    assert resp.reason == "invoke_com_error"


# ---------------------------------------------------------------------------
# (e) Finder None
# ---------------------------------------------------------------------------


def test_finder_none_disabled_by_config():
    # automation_root left None (not the _AUTOMATION_UNAVAILABLE sentinel) ->
    # the feature is genuinely off in config.
    stub = _Stub(finder=None, automation_root=None)

    captured = {"foreground_called": False}

    def _boom():
        captured["foreground_called"] = True
        raise AssertionError("must not capture foreground when finder is None")

    with patch(f"{_MOD}._capture_click_foreground", side_effect=_boom):
        UIActionHandler.click_snapshot_item(
            cast(UIActionHandler, stub),
            snapshot_id="s1", item_id="uia-3", request_id="req-9",
        )

    assert captured["foreground_called"] is False
    resp = _one_response(stub)
    assert resp.status == "error"
    assert resp.outcome == "execution_failed"
    assert resp.reason == "disabled_by_config"


def test_finder_none_automation_unavailable():
    from ui.ui_action_handler import _AUTOMATION_UNAVAILABLE

    stub = _Stub(finder=None, automation_root=_AUTOMATION_UNAVAILABLE)
    _call(stub, snapshot_id="s1", item_id="uia-3", request_id="req-9")

    resp = _one_response(stub)
    assert resp.outcome == "execution_failed"
    assert resp.reason == "automation_unavailable"


# ---------------------------------------------------------------------------
# (f) Malformed request -- no lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snapshot_id, item_id",
    [
        (5, "uia-3"),       # non-str snapshot_id
        ("s1", 7),          # non-str item_id
        ("", "uia-3"),      # empty snapshot_id
        ("s1", ""),         # empty item_id
        (None, "uia-3"),    # None snapshot_id
        ("s1", None),       # None item_id (wh-n29v.94.1)
    ],
)
def test_malformed_request_emits_invalid_request_without_lookup(snapshot_id, item_id):
    finder = _FakeFinder(_snapshot([_match("uia-3")]))
    stub = _Stub(finder=finder, executor=_FakeExecutor(
        _FakeClickResult("ok", None, "x")))

    UIActionHandler.click_snapshot_item(
        cast(UIActionHandler, stub),
        snapshot_id=snapshot_id, item_id=item_id, request_id="req-9",
    )

    resp = _one_response(stub)
    assert resp.status == "error"
    assert resp.outcome == "execution_failed"
    assert resp.reason == "invalid_request"
    # No lookup happened on a malformed request.
    assert finder.get_snapshot_calls == []


def test_non_str_trace_id_is_coerced_so_response_serialises():
    # trace_id must be a str for from_dict; a malformed trace_id must not break
    # the never-raise contract. Coerce to '' so the response still serialises.
    finder = _FakeFinder(_snapshot([_match("uia-3", name="Cancel")]))
    executor = _FakeExecutor(_FakeClickResult("ok", None, "Cancel"))
    stub = _Stub(finder=finder, executor=executor)

    _call(stub, snapshot_id="s1", item_id="uia-3", request_id="req-9",
          trace_id=cast(str, 12345))

    resp = _one_response(stub)
    assert resp.outcome == "ok"
    assert resp.trace_id == ""


# ---------------------------------------------------------------------------
# (g) Never raises; always exactly one response with request_id + action
# ---------------------------------------------------------------------------


def test_unexpected_executor_exception_maps_to_execution_failed():
    class _BoomExecutor:
        def click(self, *a, **k):
            raise RuntimeError("boom")

    finder = _FakeFinder(_snapshot([_match("uia-3")]))
    stub = _Stub(finder=finder, executor=cast(_FakeExecutor, _BoomExecutor()))

    # Must NOT raise.
    _call(stub, snapshot_id="s1", item_id="uia-3", request_id="req-9")

    resp = _one_response(stub)
    assert resp.status == "error"
    assert resp.outcome == "execution_failed"
    # Unexpected error maps to the handler's single fail-closed reason tag.
    # Pinned to the exact tag (not a permissive set) so a silently-changed
    # fail-closed tag is caught -- the notice-wording slice keys off this
    # literal string (wh-n29v.92.1).
    assert resp.reason == "invoke_com_error"


def test_get_snapshot_raising_does_not_escape():
    class _BoomFinder:
        def get_snapshot(self, *a, **k):
            raise RuntimeError("boom")

    stub = _Stub(finder=cast(_FakeFinder, _BoomFinder()),
                 executor=_FakeExecutor(_FakeClickResult("ok", None, "x")))

    _call(stub, snapshot_id="s1", item_id="uia-3", request_id="req-9")

    resp = _one_response(stub)
    assert resp.outcome == "execution_failed"
    # Pin the fail-closed reason tag for the get_snapshot-raising path too, so
    # both never-raise tests catch a silently-changed tag (wh-n29v.94.2).
    assert resp.reason == "invoke_com_error"


def test_click_snapshot_item_in_handles_own_response_allowlist():
    from input_proc import _HANDLES_OWN_RESPONSE

    assert "click_snapshot_item" in _HANDLES_OWN_RESPONSE
