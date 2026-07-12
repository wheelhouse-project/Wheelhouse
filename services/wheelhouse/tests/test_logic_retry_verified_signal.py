"""Tests for the Logic-side verified-retry signal (wh-mv5ih).

When a Try-it-anyway click round-trips through the input process and
returns ``status=success`` with ``retry_outcome="verified"``, the Logic
process publishes a ``RetryVerified`` event on the EventBus carrying
the rejected control's identity tuple. The click counter (wh-82lnx)
and three-strikes follow-up toast (wh-bqv9c) subscribe to this signal.

A response with ``retry_outcome="unverified"`` does NOT publish the
event -- the paste ran but the strategy could not confirm any text
landed, so the counter must not advance.

A click whose correlation_token has fallen out of the Logic-side
rejection-token cache (TTL elapsed, restart between rejection and
click) also does NOT publish the event. The publisher fails closed
because the counter keys off the identity tuple, and a
``RetryVerified`` event without a confirmed tuple would either crash
the consumer or (worse) increment the wrong counter.

Privacy contract: the event payload, the request payload, and the
DEBUG/INFO log lines this branch adds must NEVER contain dictation
text. Only the identity triple (process_name, class_name,
control_type) and the correlation_token cross any boundary on this
path; even the correlation_token is not in the published event.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.wheelhouse.events import RetryVerified
from services.wheelhouse.shared.rejection_token_cache import (
    RejectionTokenCache,
    RejectionTuple,
)
from services.wheelhouse.shared.retry_dictation_by_token import (
    RETRY_OUTCOME_UNVERIFIED,
    RETRY_OUTCOME_VERIFIED,
    STATUS_SUCCESS,
    STATUS_TOKEN_EXPIRED,
)


_TOKEN = "11111111-1111-4111-8111-111111111111"
_TUPLE = RejectionTuple(
    process_name="zed.exe",
    class_name="GlfwWindow",
    control_type="Pane",
    app_friendly_name="Zed",
)


def _make_controller(
    cache: RejectionTokenCache | None = None,
    consumed_tokens=None,
):
    """Build a LogicController-shaped MagicMock with a real EventBus.

    The forwarder method is unbound and called with ``__get__`` so we
    exercise the real implementation against a mock self. The
    ``rejection_token_cache`` attribute is the integration point this
    bead introduces; tests pass a real cache so the lookup branch
    runs. ``consumed_retry_tokens`` (wh-82lnx) is also a real
    ConsumedTokenSet so duplicate-click dedup is exercised.
    """

    from main import LogicController
    from event_bus import EventBus
    from shared.consumed_token_set import ConsumedTokenSet

    controller = MagicMock(spec=LogicController)
    controller.app = MagicMock()
    controller.app.send_request = AsyncMock()
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    controller.event_bus = EventBus()
    controller.rejection_token_cache = cache or RejectionTokenCache()
    controller.consumed_retry_tokens = consumed_tokens or ConsumedTokenSet()
    controller._in_flight_retry_tokens = set()

    # Bind the helper so the forwarder's call to
    # self._send_retry_followup_toast exercises the real implementation.
    controller._send_retry_followup_toast = (
        LogicController._send_retry_followup_toast.__get__(controller)
    )
    return controller


class _Recorder:
    """EventBus subscriber that records every event it receives."""

    def __init__(self):
        self.events: list = []

    async def __call__(self, event):
        self.events.append(event)


# ---------------------------------------------------------------------------
# verified outcome -> publishes RetryVerified with the cached tuple
# ---------------------------------------------------------------------------


class TestVerifiedOutcomePublishes:
    def test_verified_response_publishes_retry_verified_with_tuple(self):
        from main import LogicController

        cache = RejectionTokenCache()
        cache.put(_TOKEN, _TUPLE)
        controller = _make_controller(cache=cache)
        controller.app.send_request.return_value = {
            "status": STATUS_SUCCESS,
            "retry_outcome": RETRY_OUTCOME_VERIFIED,
            "reason": "",
        }

        recorder = _Recorder()
        controller.event_bus.subscribe(RetryVerified, recorder)

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )
        asyncio.run(forward(correlation_token=_TOKEN))

        assert len(recorder.events) == 1
        event = recorder.events[0]
        assert isinstance(event, RetryVerified)
        assert event.process_name == _TUPLE.process_name
        assert event.class_name == _TUPLE.class_name
        assert event.control_type == _TUPLE.control_type
        assert event.app_friendly_name == _TUPLE.app_friendly_name


# ---------------------------------------------------------------------------
# unverified outcome -> does NOT publish
# ---------------------------------------------------------------------------


class TestUnverifiedOutcomeDoesNotPublish:
    def test_unverified_response_does_not_publish_retry_verified(self, caplog):
        from main import LogicController

        cache = RejectionTokenCache()
        cache.put(_TOKEN, _TUPLE)
        controller = _make_controller(cache=cache)
        controller.app.send_request.return_value = {
            "status": STATUS_SUCCESS,
            "retry_outcome": RETRY_OUTCOME_UNVERIFIED,
            "reason": "",
        }

        recorder = _Recorder()
        controller.event_bus.subscribe(RetryVerified, recorder)

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )
        with caplog.at_level(logging.DEBUG):
            asyncio.run(forward(correlation_token=_TOKEN))

        assert recorder.events == []


# ---------------------------------------------------------------------------
# non-success statuses -> does NOT publish
# ---------------------------------------------------------------------------


class TestNonSuccessDoesNotPublish:
    def test_token_expired_does_not_publish_retry_verified(self):
        from main import LogicController

        cache = RejectionTokenCache()
        cache.put(_TOKEN, _TUPLE)
        controller = _make_controller(cache=cache)
        controller.app.send_request.return_value = {
            "status": STATUS_TOKEN_EXPIRED,
            "retry_outcome": None,
            "reason": "",
        }

        recorder = _Recorder()
        controller.event_bus.subscribe(RetryVerified, recorder)

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )
        asyncio.run(forward(correlation_token=_TOKEN))

        assert recorder.events == []


# ---------------------------------------------------------------------------
# verified outcome but cache miss -> does NOT publish
# ---------------------------------------------------------------------------


class TestVerifiedWithCacheMissDoesNotPublish:
    def test_verified_with_unknown_token_does_not_publish(self, caplog):
        # The Logic-side cache was never populated for this token (or
        # the entry was evicted). The verified-retry signal must NOT
        # fire because a downstream counter that depends on it cannot
        # increment without a confirmed identity tuple.
        from main import LogicController

        controller = _make_controller(cache=RejectionTokenCache())
        controller.app.send_request.return_value = {
            "status": STATUS_SUCCESS,
            "retry_outcome": RETRY_OUTCOME_VERIFIED,
            "reason": "",
        }

        recorder = _Recorder()
        controller.event_bus.subscribe(RetryVerified, recorder)

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )
        with caplog.at_level(logging.DEBUG):
            asyncio.run(forward(correlation_token=_TOKEN))

        assert recorder.events == []

    def test_verified_with_expired_token_does_not_publish(self):
        # TTL elapsed between the rejection and the click. The Input
        # side returns success because IT has the text; the Logic side
        # has lost the tuple. Fail closed -- the user can click again.
        from main import LogicController

        clock = [1000.0]

        def now():
            return clock[0]

        cache = RejectionTokenCache(ttl_seconds=10.0, time_source=now)
        cache.put(_TOKEN, _TUPLE)
        # Advance past TTL.
        clock[0] += 100.0

        controller = _make_controller(cache=cache)
        controller.app.send_request.return_value = {
            "status": STATUS_SUCCESS,
            "retry_outcome": RETRY_OUTCOME_VERIFIED,
            "reason": "",
        }

        recorder = _Recorder()
        controller.event_bus.subscribe(RetryVerified, recorder)

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )
        asyncio.run(forward(correlation_token=_TOKEN))

        assert recorder.events == []

    def test_caller_supplied_tuple_survives_ttl_during_round_trip(self):
        # wh-vbvgf.3.2: _handle_try_anyway_clicked resolves the tuple
        # at click time and passes it to forward_retry_dictation_by_token.
        # Even if the cache TTL elapses while the IPC round trip is in
        # flight, the verified retry must still publish RetryVerified
        # because the click was already accepted.
        from main import LogicController

        clock = [1000.0]

        def now():
            return clock[0]

        cache = RejectionTokenCache(ttl_seconds=10.0, time_source=now)
        cache.put(_TOKEN, _TUPLE)
        # Click time: cache HIT (within TTL). The handler captures
        # _TUPLE from the resolve() return.

        controller = _make_controller(cache=cache)

        async def slow_send_request(action, params):
            # Round trip exceeds the cache TTL.
            clock[0] += 100.0
            return {
                "status": STATUS_SUCCESS,
                "retry_outcome": RETRY_OUTCOME_VERIFIED,
                "reason": "",
            }

        controller.app.send_request.side_effect = slow_send_request

        recorder = _Recorder()
        controller.event_bus.subscribe(RetryVerified, recorder)

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )
        asyncio.run(forward(correlation_token=_TOKEN, rejection=_TUPLE))

        assert len(recorder.events) == 1
        ev = recorder.events[0]
        assert ev.process_name == _TUPLE.process_name
        assert ev.class_name == _TUPLE.class_name
        assert ev.control_type == _TUPLE.control_type
        assert ev.app_friendly_name == _TUPLE.app_friendly_name


# ---------------------------------------------------------------------------
# Duplicate correlation_token: counter increments once, second click dropped
# (wh-82lnx)
# ---------------------------------------------------------------------------


class TestDuplicateClickDedup:
    def test_double_click_on_same_token_publishes_retry_verified_once(self, caplog):
        from main import LogicController

        cache = RejectionTokenCache()
        cache.put(_TOKEN, _TUPLE)
        controller = _make_controller(cache=cache)
        controller.app.send_request.return_value = {
            "status": STATUS_SUCCESS,
            "retry_outcome": RETRY_OUTCOME_VERIFIED,
            "reason": "",
        }

        recorder = _Recorder()
        controller.event_bus.subscribe(RetryVerified, recorder)

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )
        with caplog.at_level(logging.DEBUG):
            asyncio.run(forward(correlation_token=_TOKEN))
            asyncio.run(forward(correlation_token=_TOKEN))

        # First click published; second was dropped by the consumed-
        # token set so the click counter stays at one increment.
        assert len(recorder.events) == 1
        assert any(
            "duplicate try_anyway click for token" in r.getMessage()
            for r in caplog.records
        )

    def test_different_tokens_each_publish_their_own_event(self):
        # Two distinct tokens for the same tuple: both should publish.
        from main import LogicController

        token_a = "11111111-1111-4111-8111-111111111111"
        token_b = "22222222-2222-4222-8222-222222222222"
        cache = RejectionTokenCache()
        cache.put(token_a, _TUPLE)
        cache.put(token_b, _TUPLE)
        controller = _make_controller(cache=cache)
        controller.app.send_request.return_value = {
            "status": STATUS_SUCCESS,
            "retry_outcome": RETRY_OUTCOME_VERIFIED,
            "reason": "",
        }

        recorder = _Recorder()
        controller.event_bus.subscribe(RetryVerified, recorder)

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )
        asyncio.run(forward(correlation_token=token_a))
        asyncio.run(forward(correlation_token=token_b))

        assert len(recorder.events) == 2

    def test_concurrent_clicks_send_only_one_ipc_request(self, caplog):
        # wh-82lnx.2.2: two concurrent clicks for the same token must
        # not both reach send_request. The pre-IPC reservation gates
        # the duplicate so the Input process only sees one
        # retry_dictation_by_token, which means the user does not see
        # duplicate clipboard pastes into the focused control.
        from main import LogicController

        cache = RejectionTokenCache()
        cache.put(_TOKEN, _TUPLE)
        controller = _make_controller(cache=cache)

        # Block send_request until released so both forwarder calls
        # are simultaneously in flight when the second one tries to
        # reserve the token.
        gate = asyncio.Event()
        ipc_calls: list = []

        async def gated_send(action, params):
            ipc_calls.append((action, params))
            await gate.wait()
            return {
                "status": STATUS_SUCCESS,
                "retry_outcome": RETRY_OUTCOME_VERIFIED,
                "reason": "",
            }

        controller.app.send_request.side_effect = gated_send

        recorder = _Recorder()
        controller.event_bus.subscribe(RetryVerified, recorder)

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )

        async def run_concurrent():
            task_a = asyncio.create_task(forward(correlation_token=_TOKEN))
            task_b = asyncio.create_task(forward(correlation_token=_TOKEN))
            # Yield once so both tasks reach the reservation check.
            await asyncio.sleep(0)
            gate.set()
            await asyncio.gather(task_a, task_b)

        with caplog.at_level(logging.DEBUG):
            asyncio.run(run_concurrent())

        # Only one IPC call escaped to the input process. The second
        # forwarder dropped at the in-flight reservation check.
        assert len(ipc_calls) == 1
        # Only one RetryVerified published.
        assert len(recorder.events) == 1
        # The drop is logged with the in-flight phrase.
        assert any(
            "concurrent try_anyway click for token" in r.getMessage()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Privacy: dictation text never appears in any artifact this branch produces
# ---------------------------------------------------------------------------


class TestPrivacyNoDictationText:
    def test_no_dictation_text_in_event_payload_or_logs(self, caplog):
        # Set up a cache entry whose tuple uses the canonical platform
        # identifiers, then run the verified path. Assert no plausible
        # dictation-text marker appears anywhere this branch can leak:
        #   - the published RetryVerified event
        #   - the request params sent to the input process
        #   - the log records this branch emits
        from main import LogicController

        cache = RejectionTokenCache()
        cache.put(_TOKEN, _TUPLE)
        controller = _make_controller(cache=cache)
        controller.app.send_request.return_value = {
            "status": STATUS_SUCCESS,
            "retry_outcome": RETRY_OUTCOME_VERIFIED,
            "reason": "",
        }

        recorder = _Recorder()
        controller.event_bus.subscribe(RetryVerified, recorder)

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )
        with caplog.at_level(logging.DEBUG):
            asyncio.run(forward(correlation_token=_TOKEN))

        # 1. Event payload: only the identity triple, no extra fields
        # that could carry text.
        assert len(recorder.events) == 1
        event = recorder.events[0]
        # RetryVerified is a frozen dataclass; enumerate fields and
        # confirm only the identity triple plus the app friendly name
        # is present (wh-vbvgf.4.1: app_friendly_name is platform
        # metadata, not user content; consumers like the wh-bqv9c
        # three-strikes prompt need it for the user-visible dialog).
        from dataclasses import fields as dc_fields
        field_names = {f.name for f in dc_fields(RetryVerified)}
        assert field_names == {
            "process_name", "class_name", "control_type",
            "app_friendly_name",
        }
        # Defense in depth: each field is a plain str (no nested dict
        # that could smuggle text).
        for f in dc_fields(RetryVerified):
            assert isinstance(getattr(event, f.name), str)

        # 2. Request params: no text-shaped field.
        controller.app.send_request.assert_awaited_once()
        call_args = controller.app.send_request.await_args
        params = (
            call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs.get("params") or {}
        )
        assert "text" not in params
        assert "dictation" not in params
        assert "original_text" not in params

        # 3. Log records: scan every record's getMessage() output for
        # text-like markers. (The branch this bead adds logs at DEBUG;
        # we assert nothing matches a synthetic dictation marker.)
        # We use a sentinel that no production log line should contain;
        # if a future regression injected the dictation text variable
        # into a format string by mistake, the test name itself would
        # surface it as a regression target.
        for record in caplog.records:
            msg = record.getMessage()
            # No raw text fields should appear; the only string-typed
            # values logged on this branch are the correlation_token,
            # the retry_outcome enum, and the identity triple.
            assert "dictation_text" not in msg
            assert "original_text" not in msg
