"""Tests for PluginRegistry lifecycle management.

Covers: discovery, initialization, startup, shutdown, health monitoring,
and error isolation. Tests use concrete BasePlugin subclasses rather than
mocks for the plugins themselves, since the registry interacts with the
full plugin interface.

P3-T1 of the test coverage improvement plan.
"""

import asyncio
import importlib
import logging

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock

from services.wheelhouse.plugins.base import BasePlugin, PluginState
from services.wheelhouse.plugins.registry import PluginRegistry


# ---------------------------------------------------------------------------
# Test plugin implementations
# ---------------------------------------------------------------------------

class StubPlugin(BasePlugin):
    """A well-behaved plugin for testing normal lifecycle."""

    def __init__(self, plugin_name="stub"):
        super().__init__()
        self._name = plugin_name
        self.initialize_called = False
        self.start_called = False
        self.stop_called = False

    @property
    def name(self) -> str:
        return self._name

    async def initialize(self, config, event_bus):
        self.initialize_called = True
        self._config = config
        self._event_bus = event_bus
        self._state = PluginState.INITIALIZED

    async def start(self):
        self.start_called = True
        self._state = PluginState.RUNNING

    async def stop(self):
        self.stop_called = True
        self._state = PluginState.STOPPED

    def get_health_status(self) -> dict:
        return {
            "status": "healthy" if self._state == PluginState.RUNNING else "unhealthy",
            "state": self._state.value,
        }


class FailingInitPlugin(BasePlugin):
    """Plugin that raises during initialize()."""

    @property
    def name(self) -> str:
        return "failing_init"

    async def initialize(self, config, event_bus):
        raise RuntimeError("init explosion")

    async def start(self):
        self._state = PluginState.RUNNING

    async def stop(self):
        self._state = PluginState.STOPPED

    def get_health_status(self) -> dict:
        return {"status": "unhealthy", "state": self._state.value}


class FailingStartPlugin(BasePlugin):
    """Plugin that raises during start()."""

    @property
    def name(self) -> str:
        return "failing_start"

    async def initialize(self, config, event_bus):
        self._state = PluginState.INITIALIZED

    async def start(self):
        raise RuntimeError("start explosion")

    async def stop(self):
        self._state = PluginState.STOPPED

    def get_health_status(self) -> dict:
        return {"status": "unhealthy", "state": self._state.value}


class FailingStopPlugin(BasePlugin):
    """Plugin that raises during stop()."""

    @property
    def name(self) -> str:
        return "failing_stop"

    async def initialize(self, config, event_bus):
        self._state = PluginState.INITIALIZED

    async def start(self):
        self._state = PluginState.RUNNING

    async def stop(self):
        raise RuntimeError("stop explosion")

    def get_health_status(self) -> dict:
        return {"status": "unhealthy", "state": self._state.value}


class FailingHealthPlugin(BasePlugin):
    """Plugin whose get_health_status() raises."""

    @property
    def name(self) -> str:
        return "failing_health"

    async def initialize(self, config, event_bus):
        self._state = PluginState.INITIALIZED

    async def start(self):
        self._state = PluginState.RUNNING

    async def stop(self):
        self._state = PluginState.STOPPED

    def get_health_status(self) -> dict:
        raise RuntimeError("health explosion")


class SlowPlugin(BasePlugin):
    """Plugin that blocks in start() until cancelled."""

    def __init__(self):
        super().__init__()
        self.start_entered = asyncio.Event()

    @property
    def name(self) -> str:
        return "slow"

    async def initialize(self, config, event_bus):
        self._state = PluginState.INITIALIZED

    async def start(self):
        self.start_entered.set()
        await asyncio.sleep(999)  # blocks "forever"
        self._state = PluginState.RUNNING

    async def stop(self):
        self._state = PluginState.STOPPED

    def get_health_status(self) -> dict:
        return {"status": "unhealthy", "state": self._state.value}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(mock_config, mock_event_bus):
    """Registry with mocked config and event bus."""
    return PluginRegistry(mock_config, mock_event_bus)


def _populate(registry, *plugins):
    """Helper: manually register plugins (bypasses discover_plugins)."""
    for p in plugins:
        registry.plugins[p.name] = p
        registry._plugin_order.append(p.name)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_init_stores_dependencies(self, mock_config, mock_event_bus):
        reg = PluginRegistry(mock_config, mock_event_bus)
        assert reg.config_service is mock_config
        assert reg.event_bus is mock_event_bus

    def test_init_empty_plugins(self, registry):
        assert registry.plugins == {}
        assert registry._plugin_order == []


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscoverPlugins:
    """Test plugin discovery via module scanning."""

    @pytest.mark.asyncio
    async def test_discover_handles_import_failure(self, registry):
        """If the plugins package can't be imported, discovery returns cleanly."""
        with patch("services.wheelhouse.plugins.registry.importlib.import_module",
                    side_effect=ImportError("no such package")):
            await registry.discover_plugins()
        assert len(registry.plugins) == 0

    @pytest.mark.asyncio
    async def test_discover_handles_none_file_attr(self, registry):
        """If package.__file__ is None (namespace package), discovery returns."""
        mock_pkg = MagicMock()
        mock_pkg.__file__ = None
        with patch("services.wheelhouse.plugins.registry.importlib.import_module",
                    return_value=mock_pkg):
            await registry.discover_plugins()
        assert len(registry.plugins) == 0

    @pytest.mark.asyncio
    async def test_discover_skips_disabled_plugin(self, registry, mock_config):
        """Plugins with enabled=false in config are skipped."""
        mock_config.set("plugins.stub.enabled", False)

        # Create a fake module containing StubPlugin
        fake_module = MagicMock()
        fake_module.__dict__ = {}
        # We'll set up dir() to return our plugin class
        stub_cls = type("TestStubPlugin", (BasePlugin,), {
            "name": property(lambda self: "stub"),
            "initialize": AsyncMock(),
            "start": AsyncMock(),
            "stop": AsyncMock(),
            "get_health_status": lambda self: {"status": "healthy", "state": self._state.value},
        })

        with patch("services.wheelhouse.plugins.registry.importlib.import_module") as mock_import, \
             patch("services.wheelhouse.plugins.registry.pkgutil.iter_modules",
                   return_value=[("finder", "test_stub_plugin", False)]):

            # First call gets the package, second call gets the module
            mock_pkg = MagicMock()
            mock_pkg.__file__ = "c:/fake/plugins/__init__.py"

            def import_side_effect(name):
                if name == "services.wheelhouse.plugins":
                    return mock_pkg
                mod = MagicMock()
                # Set up dir() and getattr to expose the plugin class
                mod.__dict__["TestStubPlugin"] = stub_cls
                attrs = {"TestStubPlugin": stub_cls}
                mod.__dir__ = lambda self=None: list(attrs.keys())
                type(mod).__dir__ = lambda self: list(attrs.keys())
                for k, v in attrs.items():
                    setattr(mod, k, v)
                return mod

            mock_import.side_effect = import_side_effect
            await registry.discover_plugins()

        assert "stub" not in registry.plugins

    @pytest.mark.asyncio
    async def test_discover_skips_base_registry_private_modules(self, registry):
        """Modules named 'base', 'registry', or starting with '_' produce no plugins."""
        real_import = importlib.import_module

        def safe_import(name):
            if name == "services.wheelhouse.plugins":
                mock_pkg = MagicMock()
                mock_pkg.__file__ = "c:/fake/plugins/__init__.py"
                return mock_pkg
            return real_import(name)

        with patch("services.wheelhouse.plugins.registry.importlib.import_module",
                    side_effect=safe_import), \
             patch("services.wheelhouse.plugins.registry.pkgutil.iter_modules",
                   return_value=[
                       ("finder", "base", False),
                       ("finder", "registry", False),
                       ("finder", "_private", False),
                   ]):
            await registry.discover_plugins()

        # No plugins should be discovered from reserved module names
        assert len(registry.plugins) == 0
        assert len(registry._plugin_order) == 0

    @pytest.mark.asyncio
    async def test_discover_skips_packages(self, registry):
        """Subdirectory packages are skipped (is_pkg=True)."""
        with patch("services.wheelhouse.plugins.registry.importlib.import_module") as mock_import, \
             patch("services.wheelhouse.plugins.registry.pkgutil.iter_modules",
                   return_value=[("finder", "subpkg", True)]):
            mock_pkg = MagicMock()
            mock_pkg.__file__ = "c:/fake/plugins/__init__.py"
            mock_import.return_value = mock_pkg

            await registry.discover_plugins()

        # subpkg should never be imported as a plugin module
        imported = [c.args[0] for c in mock_import.call_args_list if c.args]
        assert "services.wheelhouse.plugins.subpkg" not in imported

    @pytest.mark.asyncio
    async def test_discover_handles_module_import_error(self, registry):
        """A module that fails to import doesn't block other modules."""
        imported_modules = []
        real_import = importlib.import_module

        good_module = MagicMock()
        type(good_module).__dir__ = lambda self: []

        def tracking_import(name):
            imported_modules.append(name)
            if name == "services.wheelhouse.plugins":
                mock_pkg = MagicMock()
                mock_pkg.__file__ = "c:/fake/plugins/__init__.py"
                return mock_pkg
            if "broken_module" in name:
                raise ImportError("broken!")
            if "good_module" in name:
                return good_module
            return real_import(name)

        with patch("services.wheelhouse.plugins.registry.importlib.import_module",
                    side_effect=tracking_import), \
             patch("services.wheelhouse.plugins.registry.pkgutil.iter_modules",
                   return_value=[
                       ("finder", "broken_module", False),
                       ("finder", "good_module", False),
                   ]):
            await registry.discover_plugins()

        # Both modules were attempted despite broken_module failure
        assert "services.wheelhouse.plugins.broken_module" in imported_modules
        assert "services.wheelhouse.plugins.good_module" in imported_modules

    @pytest.mark.asyncio
    async def test_discover_handles_instantiation_error(self, registry, mock_config):
        """If a plugin class raises in __init__, discovery continues."""

        class BadInitPlugin(BasePlugin):
            def __init__(self):
                raise TypeError("cannot construct")

            @property
            def name(self): return "bad"
            async def initialize(self, c, e): pass
            async def start(self): pass
            async def stop(self): pass
            def get_health_status(self): return {}

        with patch("services.wheelhouse.plugins.registry.importlib.import_module") as mock_import, \
             patch("services.wheelhouse.plugins.registry.pkgutil.iter_modules",
                   return_value=[("finder", "bad_plugin", False)]):
            mock_pkg = MagicMock()
            mock_pkg.__file__ = "c:/fake/plugins/__init__.py"

            mod = MagicMock()
            attrs = {"BadInitPlugin": BadInitPlugin}
            type(mod).__dir__ = lambda self: list(attrs.keys())
            for k, v in attrs.items():
                setattr(mod, k, v)

            def import_side_effect(name):
                if name == "services.wheelhouse.plugins":
                    return mock_pkg
                return mod

            mock_import.side_effect = import_side_effect
            await registry.discover_plugins()

        assert len(registry.plugins) == 0


# ---------------------------------------------------------------------------
# Initialize
# ---------------------------------------------------------------------------

class TestInitializeAll:
    @pytest.mark.asyncio
    async def test_initialize_calls_plugin_initialize(self, registry, mock_config, mock_event_bus):
        """Each plugin receives config and event_bus during initialization."""
        p = StubPlugin("alpha")
        _populate(registry, p)

        with patch("services.wheelhouse.plugins.registry.get_volume_router") as mock_vr:
            mock_vr.return_value = AsyncMock()
            await registry.initialize_all()

        assert p.initialize_called
        assert p._config is mock_config
        assert p._event_bus is mock_event_bus
        assert p.state == PluginState.INITIALIZED

    @pytest.mark.asyncio
    async def test_initialize_preserves_order(self, registry):
        """Plugins are initialized in discovery order."""
        call_order = []

        class OrderedPlugin(BasePlugin):
            def __init__(self, n):
                super().__init__()
                self._name = n
            @property
            def name(self): return self._name
            async def initialize(self, c, e):
                call_order.append(self._name)
                self._state = PluginState.INITIALIZED
            async def start(self): pass
            async def stop(self): pass
            def get_health_status(self): return {}

        a, b, c = OrderedPlugin("a"), OrderedPlugin("b"), OrderedPlugin("c")
        _populate(registry, a, b, c)

        with patch("services.wheelhouse.plugins.registry.get_volume_router") as mock_vr:
            mock_vr.return_value = AsyncMock()
            await registry.initialize_all()

        assert call_order == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_initialize_marks_failed_plugin(self, registry):
        """A plugin that raises during initialize is marked FAILED."""
        p = FailingInitPlugin()
        _populate(registry, p)

        with patch("services.wheelhouse.plugins.registry.get_volume_router") as mock_vr:
            mock_vr.return_value = AsyncMock()
            await registry.initialize_all()

        assert p.state == PluginState.FAILED

    @pytest.mark.asyncio
    async def test_initialize_continues_after_failure(self, registry):
        """Failed plugins don't block initialization of subsequent plugins."""
        bad = FailingInitPlugin()
        good = StubPlugin("good")
        _populate(registry, bad, good)

        with patch("services.wheelhouse.plugins.registry.get_volume_router") as mock_vr:
            mock_vr.return_value = AsyncMock()
            await registry.initialize_all()

        assert bad.state == PluginState.FAILED
        assert good.state == PluginState.INITIALIZED

    @pytest.mark.asyncio
    async def test_initialize_handles_volume_router_failure(self, registry):
        """VolumeRouter init failure doesn't prevent plugin initialization."""
        p = StubPlugin("alpha")
        _populate(registry, p)

        with patch("services.wheelhouse.plugins.registry.get_volume_router") as mock_vr:
            vr = AsyncMock()
            vr.initialize.side_effect = RuntimeError("VR exploded")
            mock_vr.return_value = vr
            await registry.initialize_all()

        # Plugin still initialized despite VolumeRouter failure
        assert p.state == PluginState.INITIALIZED

    @pytest.mark.asyncio
    async def test_initialize_with_no_plugins(self, registry):
        """Initializing an empty registry succeeds without error."""
        with patch("services.wheelhouse.plugins.registry.get_volume_router") as mock_vr:
            mock_vr.return_value = AsyncMock()
            await registry.initialize_all()

        assert len(registry.plugins) == 0


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

class TestStartAll:
    @pytest.mark.asyncio
    async def test_start_calls_plugin_start(self, registry):
        """Initialized plugins get started."""
        p = StubPlugin("alpha")
        p._state = PluginState.INITIALIZED
        _populate(registry, p)

        await registry.start_all()

        assert p.start_called
        assert p.state == PluginState.RUNNING

    @pytest.mark.asyncio
    async def test_start_skips_failed_plugins(self, registry):
        """Plugins in FAILED state are skipped during start."""
        p = StubPlugin("alpha")
        p._state = PluginState.FAILED
        _populate(registry, p)

        await registry.start_all()

        assert not p.start_called
        assert p.state == PluginState.FAILED

    @pytest.mark.asyncio
    async def test_start_skips_uninitialized_plugins(self, registry):
        """Plugins still UNINITIALIZED are skipped during start."""
        p = StubPlugin("alpha")
        # Default state is UNINITIALIZED
        _populate(registry, p)

        await registry.start_all()

        assert not p.start_called

    @pytest.mark.asyncio
    async def test_start_marks_failed_on_error(self, registry):
        """Plugins that raise during start are marked FAILED."""
        p = FailingStartPlugin()
        p._state = PluginState.INITIALIZED
        _populate(registry, p)

        await registry.start_all()

        assert p.state == PluginState.FAILED

    @pytest.mark.asyncio
    async def test_start_continues_after_failure(self, registry):
        """One plugin failing doesn't prevent others from starting."""
        bad = FailingStartPlugin()
        bad._state = PluginState.INITIALIZED
        good = StubPlugin("good")
        good._state = PluginState.INITIALIZED
        _populate(registry, bad, good)

        await registry.start_all()

        assert bad.state == PluginState.FAILED
        assert good.state == PluginState.RUNNING

    @pytest.mark.asyncio
    async def test_start_preserves_order(self, registry):
        """Plugins are started in discovery order."""
        order = []

        class OrderPlugin(BasePlugin):
            def __init__(self, n):
                super().__init__()
                self._name = n
                self._state = PluginState.INITIALIZED
            @property
            def name(self): return self._name
            async def initialize(self, c, e): pass
            async def start(self):
                order.append(self._name)
                self._state = PluginState.RUNNING
            async def stop(self): pass
            def get_health_status(self): return {}

        _populate(registry, OrderPlugin("x"), OrderPlugin("y"), OrderPlugin("z"))
        await registry.start_all()

        assert order == ["x", "y", "z"]

    @pytest.mark.asyncio
    async def test_start_with_no_plugins(self, registry):
        """Starting an empty registry succeeds without error."""
        await registry.start_all()
        assert len(registry.plugins) == 0


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

class TestStopAll:
    @pytest.mark.asyncio
    async def test_stop_calls_plugin_stop(self, registry):
        """Running plugins get stopped."""
        p = StubPlugin("alpha")
        p._state = PluginState.RUNNING
        _populate(registry, p)

        await registry.stop_all()

        assert p.stop_called
        assert p.state == PluginState.STOPPED

    @pytest.mark.asyncio
    async def test_stop_reverse_order(self, registry):
        """Plugins are stopped in reverse discovery order."""
        order = []

        class OrderPlugin(BasePlugin):
            def __init__(self, n):
                super().__init__()
                self._name = n
                self._state = PluginState.RUNNING
            @property
            def name(self): return self._name
            async def initialize(self, c, e): pass
            async def start(self): pass
            async def stop(self):
                order.append(self._name)
                self._state = PluginState.STOPPED
            def get_health_status(self): return {}

        _populate(registry, OrderPlugin("a"), OrderPlugin("b"), OrderPlugin("c"))
        await registry.stop_all()

        assert order == ["c", "b", "a"]

    @pytest.mark.asyncio
    async def test_stop_continues_after_error(self, registry):
        """A failing stop doesn't block other plugins from stopping."""
        bad = FailingStopPlugin()
        bad._state = PluginState.RUNNING
        good = StubPlugin("good")
        good._state = PluginState.RUNNING
        # bad registered first, good second -> stop order: good, bad
        _populate(registry, bad, good)

        await registry.stop_all()

        assert good.stop_called
        assert good.state == PluginState.STOPPED

    @pytest.mark.asyncio
    async def test_stop_attempts_all_states(self, registry):
        """stop() is called even on plugins not in RUNNING state."""
        p = StubPlugin("alpha")
        p._state = PluginState.FAILED
        _populate(registry, p)

        await registry.stop_all()

        # stop() is called regardless of state
        assert p.stop_called

    @pytest.mark.asyncio
    async def test_stop_with_no_plugins(self, registry):
        """Stopping an empty registry succeeds without error."""
        await registry.stop_all()


# ---------------------------------------------------------------------------
# Health Status
# ---------------------------------------------------------------------------

class TestGetHealthStatus:
    def test_health_empty_registry(self, registry):
        """Empty registry returns zero counts."""
        status = registry.get_health_status()
        assert status["total_plugins"] == 0
        assert status["running"] == 0
        assert status["failed"] == 0
        assert status["stopped"] == 0
        assert status["plugins"] == {}

    def test_health_counts_running(self, registry):
        p = StubPlugin("alpha")
        p._state = PluginState.RUNNING
        _populate(registry, p)

        status = registry.get_health_status()
        assert status["total_plugins"] == 1
        assert status["running"] == 1
        assert status["failed"] == 0

    def test_health_counts_failed(self, registry):
        p = StubPlugin("alpha")
        p._state = PluginState.FAILED
        _populate(registry, p)

        status = registry.get_health_status()
        assert status["failed"] == 1
        assert status["running"] == 0

    def test_health_counts_stopped(self, registry):
        p = StubPlugin("alpha")
        p._state = PluginState.STOPPED
        _populate(registry, p)

        status = registry.get_health_status()
        assert status["stopped"] == 1

    def test_health_mixed_states(self, registry):
        """Multiple plugins in different states are all counted."""
        running = StubPlugin("running")
        running._state = PluginState.RUNNING
        failed = StubPlugin("failed")
        failed._state = PluginState.FAILED
        stopped = StubPlugin("stopped")
        stopped._state = PluginState.STOPPED
        _populate(registry, running, failed, stopped)

        status = registry.get_health_status()
        assert status["total_plugins"] == 3
        assert status["running"] == 1
        assert status["failed"] == 1
        assert status["stopped"] == 1

    def test_health_includes_per_plugin_status(self, registry):
        p = StubPlugin("alpha")
        p._state = PluginState.RUNNING
        _populate(registry, p)

        status = registry.get_health_status()
        assert "alpha" in status["plugins"]
        assert status["plugins"]["alpha"]["status"] == "healthy"

    def test_health_handles_failing_health_check(self, registry):
        """If a plugin's get_health_status() raises, registry doesn't crash."""
        p = FailingHealthPlugin()
        p._state = PluginState.RUNNING
        _populate(registry, p)

        status = registry.get_health_status()
        plugin_status = status["plugins"]["failing_health"]
        assert plugin_status["status"] == "unhealthy"
        assert "health explosion" in plugin_status["error"]
        # Still counted as running (by state, not health check)
        assert status["running"] == 1

    def test_health_uninitialized_not_counted_in_running_failed_stopped(self, registry):
        """UNINITIALIZED plugins appear in total but not in running/failed/stopped."""
        p = StubPlugin("alpha")
        # Default state: UNINITIALIZED
        _populate(registry, p)

        status = registry.get_health_status()
        assert status["total_plugins"] == 1
        assert status["running"] == 0
        assert status["failed"] == 0
        assert status["stopped"] == 0


# ---------------------------------------------------------------------------
# Get Plugin Status
# ---------------------------------------------------------------------------

class TestGetPluginStatus:
    def test_status_existing_plugin(self, registry):
        p = StubPlugin("alpha")
        p._state = PluginState.RUNNING
        _populate(registry, p)

        status = registry.get_plugin_status("alpha")
        assert status["status"] == "healthy"

    def test_status_unknown_plugin(self, registry):
        status = registry.get_plugin_status("nonexistent")
        assert status["status"] == "unknown"
        assert "not found" in status["error"]

    def test_status_handles_health_check_error(self, registry):
        p = FailingHealthPlugin()
        p._state = PluginState.RUNNING
        _populate(registry, p)

        status = registry.get_plugin_status("failing_health")
        assert status["status"] == "unhealthy"
        assert "health explosion" in status["error"]


# ---------------------------------------------------------------------------
# Is Plugin Running
# ---------------------------------------------------------------------------

class TestIsPluginRunning:
    def test_running_plugin(self, registry):
        p = StubPlugin("alpha")
        p._state = PluginState.RUNNING
        _populate(registry, p)

        assert registry.is_plugin_running("alpha") is True

    def test_stopped_plugin(self, registry):
        p = StubPlugin("alpha")
        p._state = PluginState.STOPPED
        _populate(registry, p)

        assert registry.is_plugin_running("alpha") is False

    def test_failed_plugin(self, registry):
        p = StubPlugin("alpha")
        p._state = PluginState.FAILED
        _populate(registry, p)

        assert registry.is_plugin_running("alpha") is False

    def test_unknown_plugin(self, registry):
        assert registry.is_plugin_running("nonexistent") is False


# ---------------------------------------------------------------------------
# Full Lifecycle Integration
# ---------------------------------------------------------------------------

class TestFullLifecycle:
    """End-to-end lifecycle: init -> start -> health -> stop."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_happy_path(self, registry, mock_config, mock_event_bus):
        """A plugin goes through the full lifecycle successfully."""
        p = StubPlugin("alpha")
        _populate(registry, p)

        with patch("services.wheelhouse.plugins.registry.get_volume_router") as mock_vr:
            mock_vr.return_value = AsyncMock()
            await registry.initialize_all()

        assert p.state == PluginState.INITIALIZED

        await registry.start_all()
        assert p.state == PluginState.RUNNING
        assert registry.is_plugin_running("alpha") is True

        health = registry.get_health_status()
        assert health["running"] == 1

        await registry.stop_all()
        assert p.state == PluginState.STOPPED
        assert registry.is_plugin_running("alpha") is False

    @pytest.mark.asyncio
    async def test_mixed_plugins_lifecycle(self, registry):
        """Registry handles a mix of healthy and failing plugins gracefully."""
        good = StubPlugin("good")
        bad_init = FailingInitPlugin()
        bad_start = FailingStartPlugin()
        _populate(registry, good, bad_init, bad_start)

        with patch("services.wheelhouse.plugins.registry.get_volume_router") as mock_vr:
            mock_vr.return_value = AsyncMock()
            await registry.initialize_all()

        assert good.state == PluginState.INITIALIZED
        assert bad_init.state == PluginState.FAILED
        assert bad_start.state == PluginState.INITIALIZED  # init succeeded

        await registry.start_all()

        assert good.state == PluginState.RUNNING
        assert bad_init.state == PluginState.FAILED  # still failed, skipped
        assert bad_start.state == PluginState.FAILED  # failed during start

        health = registry.get_health_status()
        assert health["running"] == 1
        assert health["failed"] == 2

        # Stop should still attempt all
        await registry.stop_all()
        assert good.state == PluginState.STOPPED

    @pytest.mark.asyncio
    async def test_stop_all_with_mixed_errors(self, registry):
        """Multiple stop failures don't prevent cleanup of remaining plugins."""
        first = StubPlugin("first")
        first._state = PluginState.RUNNING
        middle = FailingStopPlugin()
        middle._state = PluginState.RUNNING
        last = StubPlugin("last")
        last._state = PluginState.RUNNING
        _populate(registry, first, middle, last)

        # Stop order is: last, middle (fails), first
        await registry.stop_all()

        assert last.state == PluginState.STOPPED
        assert first.state == PluginState.STOPPED
        # middle raised but didn't block first/last


# ---------------------------------------------------------------------------
# Adversarial Tests
# ---------------------------------------------------------------------------

class TestAdversarial:
    """Tests that challenge the registry with difficult-but-realistic inputs.

    These go beyond coverage (exercising code paths) to probe whether the
    registry behaves correctly under stress and misbehavior from plugins.
    """

    def test_duplicate_plugin_names_second_overwrites_first(self, registry):
        """Two plugins with the same name: second silently overwrites first.

        This is a real discovery scenario if two modules each define a
        plugin class with the same `name` property. The current code has
        no guard against this - the dict overwrites and _plugin_order
        gets a duplicate entry. This test documents that behavior.
        """
        first = StubPlugin("dupe")
        second = StubPlugin("dupe")
        _populate(registry, first)
        # Manually register second with same name (simulating discovery)
        registry.plugins["dupe"] = second
        registry._plugin_order.append("dupe")

        # plugins dict has only the second instance
        assert registry.plugins["dupe"] is second
        # but _plugin_order has the name twice
        assert registry._plugin_order.count("dupe") == 2

    @pytest.mark.asyncio
    async def test_duplicate_name_lifecycle_operates_on_same_instance_twice(self, registry):
        """With duplicate names in _plugin_order, lifecycle methods hit the
        same plugin instance twice. Verify this doesn't crash."""
        p = StubPlugin("dupe")
        p._state = PluginState.INITIALIZED
        registry.plugins["dupe"] = p
        registry._plugin_order = ["dupe", "dupe"]

        call_count = 0
        original_start = p.start

        async def counting_start(self_ref=p):
            nonlocal call_count
            call_count += 1
            await original_start()

        p.start = counting_start
        await registry.start_all()

        # start() called once (second iteration skips: state is RUNNING, not INITIALIZED)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_start_without_initialize_skips_all(self, registry):
        """Calling start_all() without initialize_all() first: all plugins
        remain UNINITIALIZED and are skipped (not crashed)."""
        p = StubPlugin("alpha")
        _populate(registry, p)

        # Skip initialize_all(), go straight to start
        await registry.start_all()

        assert not p.start_called
        assert p.state == PluginState.UNINITIALIZED

    @pytest.mark.asyncio
    async def test_double_start_is_idempotent(self, registry):
        """Calling start_all() twice: second call skips already-RUNNING plugins."""
        p = StubPlugin("alpha")
        p._state = PluginState.INITIALIZED
        _populate(registry, p)

        await registry.start_all()
        assert p.state == PluginState.RUNNING

        # Reset tracking, call start again
        p.start_called = False
        await registry.start_all()

        # Not called again (state is RUNNING, not INITIALIZED)
        assert not p.start_called

    @pytest.mark.asyncio
    async def test_double_stop_calls_stop_twice(self, registry):
        """Calling stop_all() twice calls plugin.stop() both times.

        The registry doesn't guard against double-stop. Well-behaved plugins
        should handle this gracefully.
        """
        stop_count = 0

        class CountingStopPlugin(BasePlugin):
            @property
            def name(self): return "counter"
            async def initialize(self, c, e): pass
            async def start(self): pass
            async def stop(self):
                nonlocal stop_count
                stop_count += 1
                self._state = PluginState.STOPPED
            def get_health_status(self): return {}

        p = CountingStopPlugin()
        p._state = PluginState.RUNNING
        _populate(registry, p)

        await registry.stop_all()
        await registry.stop_all()

        assert stop_count == 2

    @pytest.mark.asyncio
    async def test_plugin_modifies_registry_during_initialize(self, registry):
        """Plugin that adds another plugin during its own initialize().

        Since initialize_all() iterates _plugin_order (a snapshot list),
        dynamically added plugins won't be initialized in the same pass.
        """

        class SneakyPlugin(BasePlugin):
            @property
            def name(self): return "sneaky"
            async def initialize(self, config, event_bus):
                # Try to sneak another plugin into the registry
                injected = StubPlugin("injected")
                registry.plugins["injected"] = injected
                registry._plugin_order.append("injected")
                self._state = PluginState.INITIALIZED
            async def start(self): self._state = PluginState.RUNNING
            async def stop(self): self._state = PluginState.STOPPED
            def get_health_status(self): return {}

        sneaky = SneakyPlugin()
        _populate(registry, sneaky)

        with patch("services.wheelhouse.plugins.registry.get_volume_router") as mock_vr:
            mock_vr.return_value = AsyncMock()
            await registry.initialize_all()

        # sneaky was initialized
        assert sneaky.state == PluginState.INITIALIZED
        # injected was added to registry...
        assert "injected" in registry.plugins
        # ...and WAS initialized (it was appended to _plugin_order which
        # is iterated, and Python iterates over appended items)
        injected = registry.plugins["injected"]
        assert injected.initialize_called

    @pytest.mark.asyncio
    async def test_start_all_blocks_on_slow_plugin(self, registry):
        """A plugin that takes a long time in start() blocks the whole
        start_all() since it's sequential, not concurrent.

        This documents that start_all is NOT timeout-protected.
        """
        slow = SlowPlugin()
        slow._state = PluginState.INITIALIZED
        fast = StubPlugin("fast")
        fast._state = PluginState.INITIALIZED
        # slow is first in order
        _populate(registry, slow, fast)

        # start_all should block on slow plugin - use timeout to prove it
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(registry.start_all(), timeout=0.05)

        # fast never got started because slow blocked
        assert not fast.start_called

    @pytest.mark.asyncio
    async def test_health_status_during_lifecycle_transition(self, registry):
        """Health status called while plugins are mid-lifecycle (STARTING state)."""
        p = StubPlugin("alpha")
        p._state = PluginState.STARTING  # mid-transition
        _populate(registry, p)

        status = registry.get_health_status()
        # STARTING is not RUNNING/FAILED/STOPPED, so counts are all zero
        assert status["total_plugins"] == 1
        assert status["running"] == 0
        assert status["failed"] == 0
        assert status["stopped"] == 0

    @pytest.mark.asyncio
    async def test_many_plugins_lifecycle(self, registry):
        """Registry handles a large number of plugins without issues."""
        plugins = [StubPlugin(f"plugin_{i}") for i in range(50)]
        _populate(registry, *plugins)

        with patch("services.wheelhouse.plugins.registry.get_volume_router") as mock_vr:
            mock_vr.return_value = AsyncMock()
            await registry.initialize_all()

        await registry.start_all()
        health = registry.get_health_status()
        assert health["running"] == 50

        await registry.stop_all()
        assert all(p.state == PluginState.STOPPED for p in plugins)
