"""Plugin discovery and lifecycle management for WheelHouse.

This module provides the PluginRegistry class, which manages the lifecycle of
all plugins in the system. It handles automatic discovery, initialization,
startup, health monitoring, and shutdown of plugins, ensuring proper dependency
injection and error isolation.

Key Classes:
  - PluginRegistry: Main plugin management system.

Key Features:
  - Automatic plugin discovery in plugins package
  - Dependency injection (ConfigService, EventBus)
  - Lifecycle management (initialize → start → stop)
  - Health monitoring and status reporting
  - Error isolation (plugin failures don't crash core)
  - Configurable plugin enable/disable

Discovery Mechanism:
  PluginRegistry automatically discovers plugin classes by scanning the
  `services.wheelhouse.plugins` package for:
  - Python modules (*.py files)
  - Classes that subclass BasePlugin
  - Classes that are not BasePlugin itself
  
  Plugins are instantiated and initialized automatically if enabled in config.

Lifecycle Management:
  1. **Discovery**: Scan plugins package for BasePlugin subclasses
  2. **Instantiation**: Create plugin instances
  3. **Initialization**: Call initialize() with config and event_bus
  4. **Startup**: Call start() on all initialized plugins
  5. **Running**: Plugins operate independently
  6. **Shutdown**: Call stop() on all plugins in reverse order
  
  Example flow:
    ```python
    # In ServiceManager
    registry = PluginRegistry(config_service, event_bus)
    await registry.discover_plugins()  # Find all plugins
    await registry.initialize_all()     # Initialize with config
    await registry.start_all()          # Start operation
    # ... application runs ...
    await registry.stop_all()           # Clean shutdown
    ```

Configuration Integration:
  Each plugin can be enabled/disabled via config.toml:
  
  ```toml
  [plugins.sonos]
  enabled = true
  speaker_ip = "192.168.1.100"
  
  [plugins.bravia]
  enabled = false  # Plugin won't be loaded
  tv_ip = "192.168.1.101"
  ```
  
  Registry checks `plugins.{plugin_name}.enabled` (defaults to true).

Error Handling Philosophy:
  Plugins should fail independently without crashing WheelHouse core:
  - Plugin discovery errors: Log and skip that plugin
  - Plugin initialization errors: Log, mark as failed, continue
  - Plugin start errors: Log, mark as failed, continue
  - Plugin runtime errors: Plugin's responsibility to handle
  
  This ensures optional integrations never break core functionality.

Health Monitoring:
  Registry provides aggregate health status across all plugins:
  
  ```python
  status = registry.get_health_status()
  # Returns:
  {
      "total_plugins": 3,
      "running": 2,
      "failed": 1,
      "plugins": {
          "sonos": {"status": "healthy", ...},
          "bravia": {"status": "failed", ...}
      }
  }
  ```

Thread Safety:
  - Registry is not thread-safe (should be used from main async loop)
  - Individual plugin operations are plugin's responsibility
  - EventBus is thread-safe for event publishing/subscribing

Typical Usage:
  ```python
  # In ServiceManager.__init__
  self.plugin_registry = PluginRegistry(
      config_service=self.config_service,
      event_bus=self.event_bus
  )
  
  # In ServiceManager initialization
  await self.plugin_registry.discover_plugins()
  await self.plugin_registry.initialize_all()
  
  # In ServiceManager.start_services
  await self.plugin_registry.start_all()
  
  # In ServiceManager.shutdown_services
  await self.plugin_registry.stop_all()
  
  # For monitoring/health checks
  health = self.plugin_registry.get_health_status()
  ```

For plugin development, see `base.py` for BasePlugin interface details.
"""
import asyncio
import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Dict, List, Type, TYPE_CHECKING

from services.wheelhouse.plugins.base import BasePlugin, PluginState
from services.wheelhouse.handlers.volume_router import get_volume_router, VolumeRouter

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.event_bus import EventBus


logger = logging.getLogger(__name__)


class PluginRegistry:
    """Manages discovery, lifecycle, and health monitoring of WheelHouse plugins.
    
    The registry is responsible for finding all available plugins, initializing
    them with proper dependencies, managing their lifecycle, and providing health
    status information. It ensures plugins fail independently and don't crash core.
    
    Attributes:
        config_service: Configuration service for plugin settings
        event_bus: Event bus for plugin communication
        plugins: Dict mapping plugin names to plugin instances
        _plugin_order: List of plugin names in initialization order
    
    Lifecycle Methods:
        discover_plugins(): Find and instantiate plugin classes
        initialize_all(): Initialize plugins with config/event_bus
        start_all(): Start all initialized plugins
        stop_all(): Stop all running plugins (reverse order)
    
    Monitoring Methods:
        get_health_status(): Get aggregate health across all plugins
        get_plugin_status(name): Get health status for specific plugin
        is_plugin_running(name): Check if specific plugin is running
    
    Example:
        ```python
        registry = PluginRegistry(config, event_bus)
        
        # Discovery and initialization
        await registry.discover_plugins()
        await registry.initialize_all()
        
        # Check what was found
        print(f"Discovered {len(registry.plugins)} plugins")
        
        # Start all plugins
        await registry.start_all()
        
        # Monitor health
        health = registry.get_health_status()
        if health["failed"] > 0:
            logger.warning(f"{health['failed']} plugins failed")
        
        # Shutdown
        await registry.stop_all()
        ```
    """
    
    def __init__(self, config_service: "ConfigService", event_bus: "EventBus"):
        """Initialize the plugin registry.
        
        Args:
            config_service: ConfigService instance for plugin configuration
            event_bus: EventBus instance for plugin communication
        """
        self.config_service = config_service
        self.event_bus = event_bus
        self.plugins: Dict[str, BasePlugin] = {}
        self._plugin_order: List[str] = []
        
        logger.info("PluginRegistry initialized")
    
    async def discover_plugins(self) -> None:
        """Discover and instantiate all available plugins.

        :flow: Plugin Lifecycle Management
        :step: 1
        :description: Auto-discovers and instantiates all enabled plugins by scanning the plugins package directory
        :data_in: Plugin package directory path
        :data_out: Populated plugin registry with enabled plugin instances
        :notes: Discovery process: (1) Scan for .py modules (2) Import and find BasePlugin subclasses (3) Check enabled status in config.toml [plugins.{name}] (4) Instantiate enabled plugins. Auto-discovery allows adding plugins without modifying core code. Error Handling: Module import errors and plugin instantiation errors are logged and skipped - discovery continues. Configuration: Plugins default to enabled unless [plugins.plugin_name] enabled=false. Side Effects: Populates self.plugins dict and self._plugin_order list, logs results.

        Example:
            ```python
            registry = PluginRegistry(config, event_bus)
            await registry.discover_plugins()
            print(f"Found {len(registry.plugins)} plugins:")
            for name in registry.plugins:
                print(f"  - {name}")
            ```
        """
        logger.info("Discovering plugins...")
        
        # Get the plugins package directory
        plugins_package = "services.wheelhouse.plugins"
        try:
            package = importlib.import_module(plugins_package)
            if package.__file__ is None:
                logger.error("Plugin package has no __file__ attribute")
                return
            package_path = Path(package.__file__).parent
        except Exception as e:
            logger.error(f"Failed to import plugins package: {e}")
            return
        
        discovered_count = 0
        
        # Iterate through all modules in the plugins package
        for _, module_name, is_pkg in pkgutil.iter_modules([str(package_path)]):
            # Skip __pycache__ and non-module items
            if is_pkg or module_name.startswith("_"):
                continue
            
            # Skip base and registry modules (not actual plugins)
            if module_name in ("base", "registry"):
                continue
            
            full_module_name = f"{plugins_package}.{module_name}"
            
            try:
                # Import the module
                module = importlib.import_module(full_module_name)
                
                # Find BasePlugin subclasses in the module
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    
                    # Check if it's a BasePlugin subclass (but not BasePlugin itself)
                    if (isinstance(attr, type) and 
                        issubclass(attr, BasePlugin) and 
                        attr is not BasePlugin):
                        
                        try:
                            # Instantiate the plugin
                            plugin_instance = attr()
                            plugin_name = plugin_instance.name
                            
                            # Check if plugin is enabled in config
                            enabled = self.config_service.get(
                                f"plugins.{plugin_name}.enabled",
                                True  # Default to enabled
                            )
                            
                            if not enabled:
                                logger.info(f"Plugin '{plugin_name}' is disabled in config, skipping")
                                continue
                            
                            # Store the plugin
                            self.plugins[plugin_name] = plugin_instance
                            self._plugin_order.append(plugin_name)
                            discovered_count += 1
                            
                            logger.info(f"Discovered plugin: {plugin_name} ({attr.__name__})")
                            
                        except Exception as e:
                            logger.error(f"Failed to instantiate plugin {attr_name}: {e}", exc_info=True)
                            continue
                
            except Exception as e:
                logger.error(f"Failed to import plugin module {full_module_name}: {e}", exc_info=True)
                continue
        
        logger.info(f"Plugin discovery complete. Found {discovered_count} enabled plugin(s).")
    
    async def initialize_all(self) -> None:
        """Initialize all discovered plugins with config and event bus.

        :flow: Plugin Lifecycle Management
        :step: 2
        :description: Initializes each discovered plugin with ConfigService and EventBus dependency injection
        :data_in: Discovered plugins, ConfigService, EventBus
        :data_out: Plugins in INITIALIZED state with loaded configuration
        :notes: Initialization process: (1) Call plugin.initialize() (2) Plugin loads config from [plugins.{name}] section (3) Plugin validates settings (4) Plugin transitions to INITIALIZED state. Failed plugins are marked FAILED but don't block others - ensures optional integrations fail independently. Error Handling: Exceptions are caught and logged, failed plugins marked FAILED, initialization continues for remaining plugins. Side Effects: Plugins receive config_service and event_bus references, transition to INITIALIZED or FAILED state, logs results.

        Example:
            ```python
            await registry.discover_plugins()
            await registry.initialize_all()

            # Check initialization results
            for name, plugin in registry.plugins.items():
                if plugin.state == PluginState.FAILED:
                    print(f"Plugin {name} failed to initialize")
            ```
        """
        logger.info(f"Initializing {len(self.plugins)} plugin(s)...")
        
        # Initialize VolumeRouter before plugins so they can query routing decisions
        volume_router = get_volume_router()
        try:
            await volume_router.initialize(self.config_service, self.event_bus)
            logger.info(f"VolumeRouter initialized: {'Sonos' if volume_router.use_sonos else 'System Volume'}")
        except Exception as e:
            logger.error(f"VolumeRouter initialization failed: {e}", exc_info=True)
        
        for plugin_name in self._plugin_order:
            plugin = self.plugins[plugin_name]
            
            try:
                logger.info(f"Initializing plugin: {plugin_name}")
                await plugin.initialize(self.config_service, self.event_bus)
                logger.info(f"Plugin '{plugin_name}' initialized successfully")
                
            except Exception as e:
                logger.error(f"Failed to initialize plugin '{plugin_name}': {e}", exc_info=True)
                plugin._state = PluginState.FAILED
        
        # Count successful initializations
        initialized = sum(1 for p in self.plugins.values() 
                         if p.state == PluginState.INITIALIZED)
        failed = sum(1 for p in self.plugins.values() 
                    if p.state == PluginState.FAILED)
        
        logger.info(f"Plugin initialization complete. Success: {initialized}, Failed: {failed}")
    
    async def start_all(self) -> None:
        """Start all initialized plugins.

        :flow: Plugin Lifecycle Management
        :step: 4
        :description: Starts each initialized plugin to begin active operation and event monitoring
        :data_in: Plugins in INITIALIZED state
        :data_out: Plugins in RUNNING state with active event subscriptions and background tasks
        :notes: Startup process: (1) Call plugin.start() (2) Plugin subscribes to EventBus events (volume, brightness commands, etc.) (3) Plugin starts background monitoring tasks (4) Plugin transitions to RUNNING state. Only INITIALIZED plugins are started, failed ones are skipped. If a plugin fails to start, it's marked FAILED but doesn't block others - enables graceful degradation. Error Handling: Exceptions caught and logged, failed plugins marked FAILED, startup continues for remaining plugins, failed plugins can be retried manually. Side Effects: Plugins begin active operation, background tasks started, event subscriptions active, logs results.

        Example:
            ```python
            await registry.start_all()

            # Check startup results
            running = sum(1 for p in registry.plugins.values()
                         if p.state == PluginState.RUNNING)
            print(f"{running} plugins running")
            ```
        """
        logger.info(f"Starting {len(self.plugins)} plugin(s)...")
        
        for plugin_name in self._plugin_order:
            plugin = self.plugins[plugin_name]
            
            # Only start plugins that initialized successfully
            if plugin.state != PluginState.INITIALIZED:
                logger.debug(f"Skipping plugin '{plugin_name}' (state: {plugin.state.value})")
                continue
            
            try:
                logger.info(f"Starting plugin: {plugin_name}")
                await plugin.start()
                logger.info(f"Plugin '{plugin_name}' started successfully")
                
            except Exception as e:
                logger.error(f"Failed to start plugin '{plugin_name}': {e}", exc_info=True)
                plugin._state = PluginState.FAILED
        
        # Count running plugins
        running = sum(1 for p in self.plugins.values() 
                     if p.state == PluginState.RUNNING)
        failed = sum(1 for p in self.plugins.values() 
                    if p.state == PluginState.FAILED)
        
        logger.info(f"Plugin startup complete. Running: {running}, Failed: {failed}")
    
    async def stop_all(self) -> None:
        """Stop all running plugins in reverse initialization order.

        :flow: Plugin Lifecycle Management
        :step: 6
        :description: Cleanly stops all plugins in reverse order during WheelHouse shutdown
        :data_in: Plugins in various states (RUNNING, FAILED, etc.)
        :data_out: Plugins in STOPPED state with resources released
        :notes: Shutdown process (reverse order): (1) Call plugin.stop() (2) Plugin cancels background tasks (3) Plugin unsubscribes from events (4) Plugin releases resources (network, files, etc.) (5) Plugin transitions to STOPPED. Reverse order handles dependencies - if plugin B uses plugin A, B stops first, preventing errors. Plugins stopped regardless of current state to ensure cleanup. Error Handling: Exceptions caught and logged, shutdown continues for remaining plugins even on error - guarantees all plugins get chance to cleanup. Side Effects: Plugins transition to STOPPED, background tasks cancelled, resources released, event subscriptions may be removed, logs results.

        Example:
            ```python
            # During application shutdown
            await registry.stop_all()

            # Verify all stopped
            stopped = sum(1 for p in registry.plugins.values()
                         if p.state == PluginState.STOPPED)
            print(f"{stopped} plugins stopped cleanly")
            ```
        """
        logger.info(f"Stopping {len(self.plugins)} plugin(s)...")
        
        # Stop in reverse order
        for plugin_name in reversed(self._plugin_order):
            plugin = self.plugins[plugin_name]
            
            try:
                logger.info(f"Stopping plugin: {plugin_name}")
                await plugin.stop()
                logger.info(f"Plugin '{plugin_name}' stopped successfully")
                
            except Exception as e:
                logger.error(f"Error stopping plugin '{plugin_name}': {e}", exc_info=True)
                # Continue stopping other plugins even if one fails
        
        stopped = sum(1 for p in self.plugins.values() 
                     if p.state == PluginState.STOPPED)
        logger.info(f"Plugin shutdown complete. Stopped: {stopped}")
    
    def get_health_status(self) -> dict:
        """Get aggregate health status across all plugins.
        
        Returns a summary of plugin health including counts by state and
        individual plugin status details.
        
        Returns:
            dict: Health status with structure:
                {
                    "total_plugins": int,
                    "running": int,
                    "failed": int,
                    "stopped": int,
                    "plugins": {
                        "plugin_name": {...health_status...},
                        ...
                    }
                }
        
        Example:
            ```python
            health = registry.get_health_status()
            
            print(f"Total plugins: {health['total_plugins']}")
            print(f"Running: {health['running']}")
            print(f"Failed: {health['failed']}")
            
            for name, status in health['plugins'].items():
                if status['status'] != 'healthy':
                    print(f"Plugin {name} is {status['status']}")
            ```
        """
        plugin_states = {
            "total_plugins": len(self.plugins),
            "running": 0,
            "failed": 0,
            "stopped": 0,
            "plugins": {}
        }
        
        for name, plugin in self.plugins.items():
            # Get plugin's health status
            try:
                plugin_health = plugin.get_health_status()
            except Exception as e:
                logger.error(f"Failed to get health status for plugin '{name}': {e}")
                plugin_health = {
                    "status": "unhealthy",
                    "state": "unknown",
                    "error": str(e)
                }
            
            plugin_states["plugins"][name] = plugin_health
            
            # Update counts
            if plugin.state == PluginState.RUNNING:
                plugin_states["running"] += 1
            elif plugin.state == PluginState.FAILED:
                plugin_states["failed"] += 1
            elif plugin.state == PluginState.STOPPED:
                plugin_states["stopped"] += 1
        
        return plugin_states
    
    def get_plugin_status(self, plugin_name: str) -> dict:
        """Get health status for a specific plugin.
        
        Args:
            plugin_name: Name of the plugin to query
        
        Returns:
            dict: Plugin health status, or error dict if plugin not found
        
        Example:
            ```python
            status = registry.get_plugin_status("sonos")
            if status["status"] == "healthy":
                print("Sonos plugin is healthy")
            ```
        """
        if plugin_name not in self.plugins:
            return {
                "status": "unknown",
                "error": f"Plugin '{plugin_name}' not found"
            }
        
        try:
            return self.plugins[plugin_name].get_health_status()
        except Exception as e:
            logger.error(f"Failed to get health status for plugin '{plugin_name}': {e}")
            return {
                "status": "unhealthy",
                "error": str(e)
            }
    
    def is_plugin_running(self, plugin_name: str) -> bool:
        """Check if a specific plugin is running.
        
        Args:
            plugin_name: Name of the plugin to check
        
        Returns:
            bool: True if plugin exists and is in RUNNING state
        
        Example:
            ```python
            if registry.is_plugin_running("sonos"):
                print("Sonos is available for commands")
            ```
        """
        if plugin_name not in self.plugins:
            return False
        
        return self.plugins[plugin_name].state == PluginState.RUNNING
