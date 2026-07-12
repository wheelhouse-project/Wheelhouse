"""WheelHouse plugin system for modular integrations.

This package provides a plugin architecture enabling third-party integrations
(smart home devices, media systems, etc.) to be added/removed without modifying
WheelHouse core code. Plugins are self-contained modules that integrate via the
EventBus and ConfigService, providing clean separation between core functionality
and optional integrations.

Key Components:
  - BasePlugin: Abstract base class defining the plugin interface.
  - PluginRegistry: Manages plugin discovery, lifecycle, and health monitoring.

Plugin Lifecycle:
  1. Discovery: PluginRegistry auto-discovers plugins in this package
  2. Initialize: Plugin receives ConfigService and EventBus references
  3. Start: Plugin begins operation (monitoring, event subscriptions, etc.)
  4. Running: Plugin responds to events and publishes state changes
  5. Stop: Plugin cleans up resources and unsubscribes from events

Plugin Design Principles:
  - **Loose coupling**: Plugins communicate via events, never direct references
  - **Fail independently**: Plugin failures don't crash WheelHouse core
  - **Configurable**: Each plugin has its own config section with enable/disable
  - **Observable**: Plugins report health status for monitoring
  - **Testable**: Plugins can be tested in isolation with mock EventBus

Typical Plugin Structure:
  ```python
  class MyPlugin(BasePlugin):
      @property
      def name(self) -> str:
          return "my_plugin"
      
      async def initialize(self, config: ConfigService, event_bus: EventBus):
          self.config = config
          self.event_bus = event_bus
          # Load plugin-specific configuration
      
      async def start(self):
          # Subscribe to events, start monitoring tasks
          self.event_bus.subscribe(SomeEvent, self.handle_event)
      
      async def stop(self):
          # Cleanup and unsubscribe
          pass
      
      def get_health_status(self) -> dict:
          return {"status": "healthy"}
  ```

Integration with WheelHouse:
  - ServiceManager creates PluginRegistry during initialization
  - PluginRegistry discovers and initializes all plugins
  - Plugins integrate via EventBus (no direct service dependencies)
  - Configuration lives in `config.toml` under `[plugins.*]` sections

For plugin development guide, see `base.py` docstrings.
"""

from services.wheelhouse.plugins.base import BasePlugin
from services.wheelhouse.plugins.registry import PluginRegistry

__all__ = ["BasePlugin", "PluginRegistry"]
