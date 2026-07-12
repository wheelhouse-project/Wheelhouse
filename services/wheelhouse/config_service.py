"""Manages static application configuration using a hybrid strategy.

This module provides the ConfigService, which is responsible for managing
the application's static configuration. It embodies the "pull" part of a
hybrid configuration model:

- **Static "Pull" Configuration (This Service):** For startup-critical
  parameters that are read once and rarely change. This synchronous model
  ensures stability, as failures in loading critical configuration are
  fatal and prevent the application from starting in an invalid state.

- **Dynamic "Push" Configuration (EventBus):** For runtime-changeable
  settings (e.g., `commands.toml`). The EventBus pushes notifications to
  interested services when these settings are modified.

This service uses the TOML format for its configuration files to improve
readability and allow for inline comments, treating configuration as a
form of documentation.
"""
import asyncio
import logging
import tomllib
from typing import Any, Dict
import os

logger = logging.getLogger(__name__)

class ConfigService:
    """
    A service to manage application configuration.

    It reads configuration from a TOML file and provides
    a simple interface to access configuration values.
    """
    _config: Dict[str, Any] = {}

    def __init__(self, config_path: str = None):
        """
        Initializes the ConfigService.

        Args:
            config_path: The path to the configuration file. If None, it defaults
                         to 'config.toml' in the same directory as this file.
        """
        if config_path is None:
            # Default to config.toml in the same directory as this script
            base_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(base_dir, "config.toml")
        
        self.config_path = config_path
        self.load_config(self.config_path)

    def load_config(self, config_path: str):
        """:flow: Configuration Loading
        :step: 1
        :description: Load and parse TOML configuration file
        :data_in: config_path (absolute path to config.toml)
        :data_out: Parsed configuration dictionary stored in self._config
        :notes: Startup-critical configuration loading. Opens config.toml in binary mode, parses via tomllib.load() into nested dictionary structure. Uses 'pull' model - synchronous, fail-fast loading. If file not found or invalid TOML, raises exception to prevent app startup with invalid config. This ensures stable, validated configuration before any services initialize. For runtime-changeable settings (commands.toml), use EventBus 'push' model instead.
        
        Args:
            config_path: The path to the configuration file.

        Raises:
            FileNotFoundError: If the configuration file cannot be found.
            ValueError: If the configuration file is not valid TOML.
        """
        try:
            with open(config_path, "rb") as f:
                self._config = tomllib.load(f)
        except FileNotFoundError as e:
            logger.error(f"Error: Configuration file not found at {config_path}")
            raise e
        except tomllib.TOMLDecodeError as e:
            logger.error(f"Error: Could not decode TOML from {config_path}")
            raise ValueError(f"Invalid TOML format in {config_path}") from e

    def get_config(self) -> Dict[str, Any]:
        """Returns the entire configuration dictionary.
        
        Returns:
            Dict containing full configuration tree
        """
        return self._config

    def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieves a configuration value for a given key.
        
        Supports dot notation for nested keys (e.g., "plugins.bravia.device_name").

        Args:
            key: The configuration key to retrieve. Use dots for nested keys.
            default: The default value to return if the key is not found.

        Returns:
            The configuration value, or the default if not found.
        """
        # Handle dot notation for nested keys
        if "." in key:
            keys = key.split(".")
            value = self._config
            for k in keys:
                if isinstance(value, dict):
                    value = value.get(k)
                    if value is None:
                        return default
                else:
                    return default
            return value
        
        # Simple key lookup
        return self._config.get(key, default)

    def set(self, key: str, value: Any):
        """
        Sets a configuration value in memory.
        
        Supports dot notation for nested keys (e.g., "stt.mode").

        Args:
            key: The configuration key to set. Use dots for nested keys.
            value: The value to set.
        """
        # Handle dot notation for nested keys
        if "." in key:
            keys = key.split(".")
            target = self._config
            for k in keys[:-1]:
                if k not in target:
                    target[k] = {}
                target = target[k]
            target[keys[-1]] = value
        else:
            self._config[key] = value

    async def save(self):
        """
        Saves the current configuration to the TOML file.
        This is an async method that can be awaited.
        """
        import tomli_w
        
        def do_save():
            """Synchronous file write operation for TOML config.
            
            Runs in thread pool via asyncio.to_thread to avoid blocking.
            """
            try:
                with open(self.config_path, "wb") as f:
                    tomli_w.dump(self._config, f)
                logger.info(f"Configuration saved to {self.config_path}")
            except Exception as e:
                logger.error(f"Failed to save configuration: {e}")

        # Run the synchronous file I/O in a separate thread
        await asyncio.to_thread(do_save)

# Example of how to use it (optional, for testing)
if __name__ == "__main__":
    async def main_test():
        # Setup basic logging for the test
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        
        config_service = ConfigService()
        print(f"Bravia IP: {config_service.get('BRAVIA_IP')}")
        print(f"Original Log Level: {config_service.get('LOG_LEVEL', 'INFO')}")
        
        # Test setting and saving
        print("Setting LOG_LEVEL to DEBUG and saving...")
        config_service.set("LOG_LEVEL", "DEBUG")
        await config_service.save()
        
        # Verify by reloading
        print("Reloading configuration to verify save...")
        new_config_service = ConfigService()
        print(f"New Log Level from file: {new_config_service.get('LOG_LEVEL')}")

        # Revert the change
        print("Reverting LOG_LEVEL to INFO...")
        new_config_service.set("LOG_LEVEL", "INFO")
        await new_config_service.save()
        print("Change reverted.")

    asyncio.run(main_test())
