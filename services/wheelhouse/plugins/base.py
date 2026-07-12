"""Base plugin interface for WheelHouse integrations.

This module defines the abstract base class that all WheelHouse plugins must
implement. It establishes the contract between the plugin system and individual
plugins, ensuring consistent lifecycle management, configuration handling, and
health monitoring across all integrations.

Key Classes:
  - BasePlugin: Abstract base class for all plugins.
  - PluginState: Enum representing plugin lifecycle states.

Plugin Lifecycle States:
  - UNINITIALIZED: Plugin created but not yet initialized
  - INITIALIZED: Plugin has config/event_bus, ready to start
  - STARTING: Plugin is in the process of starting up
  - RUNNING: Plugin is fully operational
  - STOPPING: Plugin is shutting down
  - STOPPED: Plugin has completed shutdown
  - FAILED: Plugin encountered an unrecoverable error

Plugin Implementation Requirements:
  - Must subclass BasePlugin
  - Must implement all abstract methods
  - Should handle errors gracefully (don't crash core)
  - Should validate configuration in initialize()
  - Should clean up resources in stop()
  - Should report accurate health status

Event Integration Pattern:
  Plugins integrate with WheelHouse via EventBus, not direct service references.
  This ensures loose coupling and testability.
  
  Example event patterns:
  - Subscribe to commands: `event_bus.subscribe(VolumeUpCommand, self.handle_volume_up)`
  - Publish state changes: `event_bus.publish(VolumeChangedEvent(volume=50))`
  - Listen for system events: `event_bus.subscribe(SystemShutdownEvent, self.cleanup)`

Configuration Pattern:
  Plugins read configuration from their own section in config.toml:
  
  ```toml
  [plugins.my_plugin]
  enabled = true
  device_ip = "192.168.1.100"
  polling_interval = 5
  ```
  
  Access in plugin:
  ```python
  async def initialize(self, config: ConfigService, event_bus: EventBus):
      self.enabled = config.get("plugins.my_plugin.enabled", True)
      self.device_ip = config.get("plugins.my_plugin.device_ip", "")
      if not self.device_ip:
          raise ValueError("device_ip is required")
  ```

Health Status Pattern:
  Plugins should return a dictionary with status information:
  
  ```python
  def get_health_status(self) -> dict:
      return {
          "status": "healthy" | "degraded" | "unhealthy",
          "state": self._state.value,
          "last_check": timestamp,
          "details": {
              "connected": True,
              "device_ip": "192.168.1.100",
              "error": None
          }
      }
  ```

Error Handling Pattern:
  Plugins should catch and log errors rather than propagating them:
  
  ```python
  async def start(self):
      try:
          await self._connect_to_device()
          self._state = PluginState.RUNNING
      except Exception as e:
          logger.error(f"{self.name} failed to start: {e}")
          self._state = PluginState.FAILED
          # Don't raise - let plugin fail independently
  ```

For implementation examples, see:
  - `plugins/sonos_plugin.py` - Media system integration
  - `plugins/bravia_plugin.py` - Display control integration
"""
import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.event_bus import EventBus


logger = logging.getLogger(__name__)


class PluginState(Enum):
    """Represents the lifecycle state of a plugin."""
    UNINITIALIZED = "uninitialized"
    INITIALIZED = "initialized"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class BasePlugin(ABC):
    """Abstract base class for all WheelHouse plugins.
    
    Plugins are modular integrations that extend WheelHouse functionality without
    requiring changes to core code. Each plugin manages a specific integration
    (smart home device, media system, etc.) and communicates via EventBus.
    
    Subclasses must implement:
      - name: Property returning unique plugin identifier
      - initialize(): Setup with config and event_bus references
      - start(): Begin plugin operation (subscriptions, monitoring)
      - stop(): Cleanup and resource release
      - get_health_status(): Report current health state
    
    Lifecycle:
      1. Plugin instantiated by PluginRegistry
      2. initialize() called with config and event_bus
      3. start() called to begin operation
      4. Plugin operates, responding to events
      5. stop() called during shutdown
    
    Thread Safety:
      Plugins operate in an asyncio context. Use async methods for I/O operations.
      EventBus is thread-safe. Config reads are thread-safe (immutable after load).
    
    Example Implementation:
      ```python
      class MyDevicePlugin(BasePlugin):
          def __init__(self):
              self._state = PluginState.UNINITIALIZED
              self._config = None
              self._event_bus = None
              self._monitor_task = None
          
          @property
          def name(self) -> str:
              return "my_device"
          
          async def initialize(self, config: ConfigService, event_bus: EventBus):
              self._config = config
              self._event_bus = event_bus
              
              # Validate required configuration
              self.device_ip = config.get("plugins.my_device.device_ip")
              if not self.device_ip:
                  raise ValueError("device_ip configuration is required")
              
              self._state = PluginState.INITIALIZED
              logger.info(f"{self.name} initialized")
          
          async def start(self):
              try:
                  self._state = PluginState.STARTING
                  
                  # Subscribe to relevant events
                  self._event_bus.subscribe(DeviceCommandEvent, self._handle_command)
                  
                  # Start monitoring task
                  self._monitor_task = asyncio.create_task(self._monitor_device())
                  
                  self._state = PluginState.RUNNING
                  logger.info(f"{self.name} started")
              except Exception as e:
                  logger.error(f"{self.name} failed to start: {e}")
                  self._state = PluginState.FAILED
          
          async def stop(self):
              self._state = PluginState.STOPPING
              
              # Cancel monitoring task
              if self._monitor_task:
                  self._monitor_task.cancel()
              
              # Cleanup would go here
              
              self._state = PluginState.STOPPED
              logger.info(f"{self.name} stopped")
          
          def get_health_status(self) -> dict:
              return {
                  "status": "healthy" if self._state == PluginState.RUNNING else "unhealthy",
                  "state": self._state.value,
                  "device_ip": self.device_ip
              }
          
          async def _handle_command(self, event: DeviceCommandEvent):
              # Handle commands from EventBus
              pass
          
          async def _monitor_device(self):
              # Monitoring loop
              while self._state == PluginState.RUNNING:
                  # Check device state, publish events
                  await asyncio.sleep(5)
      ```
    """
    
    def __init__(self):
        """Initialize base plugin state.
        
        Subclasses should call super().__init__() and initialize their own state.
        """
        self._state = PluginState.UNINITIALIZED
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Return the unique identifier for this plugin.
        
        The name should be:
          - Lowercase with underscores (e.g., "sonos_control")
          - Unique across all plugins
          - Used in configuration sections: [plugins.{name}]
          - Used in logging and health reporting
        
        Returns:
            str: Unique plugin identifier
        """
        pass
    
    @abstractmethod
    async def initialize(self, config: "ConfigService", event_bus: "EventBus") -> None:
        """Initialize the plugin with configuration and event bus.
        
        :flow: Plugin Lifecycle Management
        :step: 3
        :description: Abstract method for plugin-specific initialization logic
        :data_in: ConfigService, EventBus
        :data_out: Initialized plugin state
        :notes: Implementation requirements: (1) Store config/event_bus references (2) Load and validate configuration (3) Initialize internal state (4) Set state to INITIALIZED. Must NOT start background tasks or subscriptions here (use start()).

        This method is called once after plugin instantiation, before start().
        Use this to:
          - Store config and event_bus references
          - Load plugin-specific configuration
          - Validate required configuration values
          - Initialize internal state
          - Create (but don't start) background tasks
        
        DO NOT:
          - Start background tasks (do that in start())
          - Subscribe to events (do that in start())
          - Make network connections (do that in start())
        
        Args:
            config: ConfigService instance for reading configuration
            event_bus: EventBus instance for event communication
        
        Raises:
            ValueError: If required configuration is missing or invalid
            Any other exception indicates initialization failure
        """
        pass
    
    @abstractmethod
    async def start(self) -> None:
        """Start plugin operation.
        
        :flow: Plugin Lifecycle Management
        :step: 5
        :description: Abstract method for plugin-specific startup logic
        :data_in: None
        :data_out: Running plugin state
        :notes: Implementation requirements: (1) Subscribe to EventBus events (2) Start background tasks/loops (3) Connect to external devices (4) Set state to RUNNING. Must handle startup failures gracefully by setting state to FAILED.

        This method is called after initialize() when WheelHouse is ready for
        plugins to begin active operation. Use this to:
          - Subscribe to EventBus events
          - Start background monitoring tasks
          - Establish device connections
          - Begin polling/monitoring loops
        
        Error Handling:
          - Catch exceptions and set state to FAILED
          - Log errors but don't raise (fail independently)
          - Update health status to reflect failure
        
        State Management:
          - Set state to STARTING at beginning
          - Set state to RUNNING on success
          - Set state to FAILED on error
        
        Example:
            ```python
            async def start(self):
                try:
                    self._state = PluginState.STARTING
                    self._event_bus.subscribe(CommandEvent, self._handle_command)
                    self._task = asyncio.create_task(self._monitor())
                    self._state = PluginState.RUNNING
                except Exception as e:
                    logger.error(f"Failed to start: {e}")
                    self._state = PluginState.FAILED
            ```
        """
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """Stop plugin operation and clean up resources.
        
        :flow: Plugin Lifecycle Management
        :step: 7
        :description: Abstract method for plugin-specific shutdown logic
        :data_in: None
        :data_out: Stopped plugin state
        :notes: Implementation requirements: (1) Cancel background tasks (2) Unsubscribe from events (3) Disconnect from devices (4) Release resources (5) Set state to STOPPED. Must ensure cleanup completes even if errors occur.

        This method is called during WheelHouse shutdown. Use this to:
          - Cancel background tasks
          - Unsubscribe from events (if EventBus supports it)
          - Close device connections
          - Release resources
        
        Error Handling:
          - Catch and log exceptions
          - Ensure cleanup completes even on error
          - Set state to STOPPED when done
        
        State Management:
          - Set state to STOPPING at beginning
          - Set state to STOPPED on completion
        
        Example:
            ```python
            async def stop(self):
                self._state = PluginState.STOPPING
                if self._task:
                    self._task.cancel()
                    try:
                        await self._task
                    except asyncio.CancelledError:
                        pass
                self._state = PluginState.STOPPED
            ```
        """
        pass
    
    @abstractmethod
    def get_health_status(self) -> dict:
        """Return current health and status information.
        
        This method is called periodically to check plugin health. Return a
        dictionary with status information that can be used for monitoring,
        debugging, and health checks.
        
        Required keys:
          - status: "healthy" | "degraded" | "unhealthy"
          - state: Current PluginState value
        
        Optional keys:
          - last_check: Timestamp of last health check
          - error: Error message if status is degraded/unhealthy
          - details: Plugin-specific status details
        
        Returns:
            dict: Health status information
        
        Example:
            ```python
            def get_health_status(self) -> dict:
                return {
                    "status": "healthy" if self._state == PluginState.RUNNING else "unhealthy",
                    "state": self._state.value,
                    "last_check": time.time(),
                    "details": {
                        "device_connected": self._is_connected,
                        "device_ip": self.device_ip,
                        "last_response": self._last_response_time
                    }
                }
            ```
        """
        pass
    
    @property
    def state(self) -> PluginState:
        """Return current plugin lifecycle state.
        
        Returns:
            PluginState: Current state of the plugin
        """
        return self._state
