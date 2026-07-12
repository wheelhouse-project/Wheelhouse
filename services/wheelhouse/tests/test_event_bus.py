"""Tests for EventBus - typed publish/subscribe event system.

Covers:
- subscribe + publish with typed dataclass events
- Multiple subscribers receive same event
- Unrelated event types don't cross-fire
- Handler exception isolation (return_exceptions=True behavior)
- Async handlers are awaited concurrently
"""

from dataclasses import dataclass

import pytest

from event_bus import EventBus


@dataclass(frozen=True)
class AlphaEvent:
    value: str


@dataclass(frozen=True)
class BetaEvent:
    count: int


# -----------------------------------------------------------------------
# Basic subscribe/publish
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_delivers_to_subscriber():
    bus = EventBus()
    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe(AlphaEvent, handler)
    await bus.publish(AlphaEvent(value="hello"))

    assert len(received) == 1
    assert received[0].value == "hello"


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_noop():
    bus = EventBus()
    # Should not raise
    await bus.publish(AlphaEvent(value="ignored"))


@pytest.mark.asyncio
async def test_publish_ignores_unrelated_event_types():
    bus = EventBus()
    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe(AlphaEvent, handler)
    await bus.publish(BetaEvent(count=42))

    assert len(received) == 0


# -----------------------------------------------------------------------
# Multiple subscribers
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_subscribers_all_receive():
    bus = EventBus()
    results_a, results_b = [], []

    async def handler_a(event):
        results_a.append(event)

    async def handler_b(event):
        results_b.append(event)

    bus.subscribe(AlphaEvent, handler_a)
    bus.subscribe(AlphaEvent, handler_b)
    await bus.publish(AlphaEvent(value="shared"))

    assert len(results_a) == 1
    assert len(results_b) == 1
    assert results_a[0].value == "shared"


@pytest.mark.asyncio
async def test_subscribers_isolated_by_type():
    bus = EventBus()
    alpha_received, beta_received = [], []

    async def alpha_handler(event):
        alpha_received.append(event)

    async def beta_handler(event):
        beta_received.append(event)

    bus.subscribe(AlphaEvent, alpha_handler)
    bus.subscribe(BetaEvent, beta_handler)

    await bus.publish(AlphaEvent(value="a"))
    await bus.publish(BetaEvent(count=1))

    assert len(alpha_received) == 1
    assert len(beta_received) == 1


# -----------------------------------------------------------------------
# Exception behavior (handler isolation)
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handler_exception_does_not_propagate():
    """A bad handler's exception is caught and logged, not propagated."""
    bus = EventBus()

    async def bad_handler(event):
        raise ValueError("boom")

    bus.subscribe(AlphaEvent, bad_handler)

    # publish() should NOT raise -- the exception is isolated
    await bus.publish(AlphaEvent(value="x"))


@pytest.mark.asyncio
async def test_one_bad_handler_does_not_affect_good_handler():
    """A bad handler does not prevent the good handler from receiving the event."""
    bus = EventBus()
    received = []

    async def good_handler(event):
        received.append(event)

    async def bad_handler(event):
        raise RuntimeError("fail")

    bus.subscribe(AlphaEvent, good_handler)
    bus.subscribe(AlphaEvent, bad_handler)

    # publish() should NOT raise
    await bus.publish(AlphaEvent(value="test"))

    # Good handler still received the event
    assert len(received) == 1
    assert received[0].value == "test"


# -----------------------------------------------------------------------
# Concurrent execution
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handlers_run_concurrently():
    """Verify asyncio.gather runs handlers concurrently, not sequentially."""
    import asyncio

    bus = EventBus()
    order = []

    async def slow_handler(event):
        order.append("slow_start")
        await asyncio.sleep(0.05)
        order.append("slow_end")

    async def fast_handler(event):
        order.append("fast_start")
        order.append("fast_end")

    bus.subscribe(AlphaEvent, slow_handler)
    bus.subscribe(AlphaEvent, fast_handler)
    await bus.publish(AlphaEvent(value="race"))

    # Both started before slow finishes (concurrent)
    assert order.index("fast_start") < order.index("slow_end")


# -----------------------------------------------------------------------
# Multiple publishes
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_publishes_each_delivered():
    bus = EventBus()
    received = []

    async def handler(event):
        received.append(event.value)

    bus.subscribe(AlphaEvent, handler)
    await bus.publish(AlphaEvent(value="first"))
    await bus.publish(AlphaEvent(value="second"))
    await bus.publish(AlphaEvent(value="third"))

    assert received == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_subscribe_same_handler_twice_receives_twice():
    bus = EventBus()
    count = []

    async def handler(event):
        count.append(1)

    bus.subscribe(AlphaEvent, handler)
    bus.subscribe(AlphaEvent, handler)
    await bus.publish(AlphaEvent(value="x"))

    assert len(count) == 2


# -----------------------------------------------------------------------
# Adversarial: subscribe during publish
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_during_publish_does_not_affect_current_dispatch():
    """A handler that subscribes a new handler during dispatch should not
    cause the new handler to fire in the same publish cycle.

    publish() builds the callback list eagerly via list comprehension before
    asyncio.gather awaits them, so mutations during dispatch are safe for the
    current cycle.
    """
    bus = EventBus()
    late_received = []

    async def late_handler(event):
        late_received.append(event)

    async def subscribing_handler(event):
        # Subscribe a new handler mid-dispatch
        bus.subscribe(AlphaEvent, late_handler)

    bus.subscribe(AlphaEvent, subscribing_handler)
    await bus.publish(AlphaEvent(value="first"))

    # late_handler was NOT called during the first publish
    assert len(late_received) == 0

    # But it IS registered now, so a second publish delivers to it
    await bus.publish(AlphaEvent(value="second"))
    assert len(late_received) == 1
    assert late_received[0].value == "second"


# -----------------------------------------------------------------------
# Adversarial: non-callable subscriber
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_callable_subscriber_does_not_crash_publish():
    """subscribe() does not validate callability. Passing a non-callable
    succeeds at subscribe time. With handler isolation, the TypeError is
    caught and logged rather than propagated to the caller.
    """
    bus = EventBus()

    # subscribe() accepts anything -- no validation
    bus.subscribe(AlphaEvent, "not_a_function")

    # publish() should NOT raise -- the error is isolated and logged
    await bus.publish(AlphaEvent(value="x"))
