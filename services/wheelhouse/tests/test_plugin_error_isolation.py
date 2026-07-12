"""Tests for plugin error isolation -- plugin failures must not cascade.

For accessibility users, speech recognition must survive any single plugin
failure.  These tests verify that:

1. EventBus handler exceptions are isolated via ``return_exceptions=True`` --
   a failing handler never disrupts the publish() caller or sibling handlers.
2. PluginRegistry.start_all / stop_all isolate individual plugin failures so
   one broken plugin never prevents the rest from starting or stopping.
"""

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from event_bus import EventBus
from services.wheelhouse.plugins.base import BasePlugin, PluginState


# -----------------------------------------------------------------------
# Test events
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class IsolationEvent:
    payload: str


# -----------------------------------------------------------------------
# Concrete test plugin
# -----------------------------------------------------------------------

class StubPlugin(BasePlugin):
    """Minimal concrete plugin for registry tests.

    Parameters control whether initialize / start / stop raise.
    """

    def __init__(
        self,
        plugin_name: str = "stub",
        *,
        fail_on_start: bool = False,
        fail_on_stop: bool = False,
    ):
        super().__init__()
        self._name = plugin_name
        self._fail_on_start = fail_on_start
        self._fail_on_stop = fail_on_stop
        self.started = False
        self.stopped = False

    @property
    def name(self) -> str:
        return self._name

    async def initialize(self, config, event_bus):
        self._state = PluginState.INITIALIZED

    async def start(self):
        if self._fail_on_start:
            raise RuntimeError(f"{self._name} start exploded")
        self._state = PluginState.RUNNING
        self.started = True

    async def stop(self):
        if self._fail_on_stop:
            raise RuntimeError(f"{self._name} stop exploded")
        self._state = PluginState.STOPPED
        self.stopped = True

    def get_health_status(self) -> dict:
        return {
            "status": "healthy" if self._state == PluginState.RUNNING else "unhealthy",
            "state": self._state.value,
        }


# =======================================================================
# TestEventBusErrorIsolation
# =======================================================================

class TestEventBusExceptionPropagation:
    """Verify EventBus handler isolation -- handler failures are contained.

    The EventBus uses ``asyncio.gather(*tasks, return_exceptions=True)``
    so handler exceptions are caught and logged rather than propagated to
    the caller. This ensures a bug in one handler never disrupts the
    publish() caller or prevents sibling handlers from executing.
    """

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_propagate_to_caller(self):
        """A failing handler's exception is caught and logged, not propagated.

        All handlers still execute concurrently, and the caller does NOT
        see the exception. The good handler receives the event normally.
        """
        bus = EventBus()
        good_received = []

        async def good_handler(event):
            good_received.append(event.payload)

        async def bad_handler(event):
            raise ValueError("handler kaboom")

        bus.subscribe(IsolationEvent, good_handler)
        bus.subscribe(IsolationEvent, bad_handler)

        # publish() should NOT raise -- handler exceptions are isolated
        await bus.publish(IsolationEvent(payload="test"))

        # Good handler still received the event
        assert good_received == ["test"]

    @pytest.mark.asyncio
    async def test_all_handlers_execute_despite_exceptions(self):
        """All handlers run even when multiple raise. Exceptions are caught
        and logged, and publish() does not raise.
        """
        bus = EventBus()
        call_log = []

        async def bad_handler_a(event):
            call_log.append("bad_a")
            raise RuntimeError("fail A")

        async def good_handler(event):
            call_log.append("good")

        async def bad_handler_b(event):
            call_log.append("bad_b")
            raise RuntimeError("fail B")

        bus.subscribe(IsolationEvent, bad_handler_a)
        bus.subscribe(IsolationEvent, good_handler)
        bus.subscribe(IsolationEvent, bad_handler_b)

        # publish() should NOT raise -- all exceptions are isolated
        await bus.publish(IsolationEvent(payload="multi"))

        # All three handlers executed
        assert "bad_a" in call_log
        assert "good" in call_log
        assert "bad_b" in call_log


# =======================================================================
# TestPluginRegistryErrorIsolation
# =======================================================================

class TestPluginRegistryErrorIsolation:
    """Verify PluginRegistry isolates individual plugin failures.

    Unlike EventBus (which uses bare asyncio.gather), the registry wraps
    each plugin lifecycle call in try/except, so one plugin crashing must
    never prevent others from starting or stopping.
    """

    @staticmethod
    def _build_registry(plugins, mock_config):
        """Build a PluginRegistry with pre-loaded plugins, bypassing
        discover_plugins() which scans the real filesystem.
        """
        from services.wheelhouse.plugins.registry import PluginRegistry

        registry = PluginRegistry(
            config_service=mock_config,
            event_bus=EventBus(),
        )
        for p in plugins:
            registry.plugins[p.name] = p
            registry._plugin_order.append(p.name)
        return registry

    @pytest.mark.asyncio
    async def test_plugin_start_failure_doesnt_crash_registry(self, mock_config):
        """A plugin that raises during start() must not prevent other
        plugins from starting.
        """
        good_a = StubPlugin("good_a")
        bad   = StubPlugin("bad", fail_on_start=True)
        good_b = StubPlugin("good_b")

        # Initialize all so they reach INITIALIZED state.
        for p in (good_a, bad, good_b):
            await p.initialize(mock_config, None)

        registry = self._build_registry([good_a, bad, good_b], mock_config)
        await registry.start_all()

        # Good plugins reached RUNNING.
        assert good_a.state == PluginState.RUNNING
        assert good_b.state == PluginState.RUNNING
        assert good_a.started is True
        assert good_b.started is True

        # Bad plugin was marked FAILED, not RUNNING.
        assert bad.state == PluginState.FAILED

        # Health status reflects the failure accurately.
        health = registry.get_health_status()
        assert health["running"] == 2
        assert health["failed"] == 1

    @pytest.mark.asyncio
    async def test_plugin_stop_failure_doesnt_prevent_other_stops(self, mock_config):
        """A plugin that raises during stop() must not prevent other
        plugins from stopping.
        """
        good_a = StubPlugin("good_a")
        bad   = StubPlugin("bad", fail_on_stop=True)
        good_b = StubPlugin("good_b")

        # Move all through initialize -> start so they are RUNNING.
        for p in (good_a, bad, good_b):
            await p.initialize(mock_config, None)
            await p.start()

        assert all(
            p.state == PluginState.RUNNING for p in (good_a, bad, good_b)
        )

        registry = self._build_registry([good_a, bad, good_b], mock_config)
        await registry.stop_all()

        # Good plugins stopped cleanly.
        assert good_a.state == PluginState.STOPPED
        assert good_b.state == PluginState.STOPPED
        assert good_a.stopped is True
        assert good_b.stopped is True

        # Bad plugin is still RUNNING (stop() raised before it could
        # transition to STOPPED).
        assert bad.state == PluginState.RUNNING
