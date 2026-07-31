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

# Default remote STT provider used when stt.last_provider is absent from config.
# Must match the value config.toml.example ships (guarded by
# tests/test_stt_default_provider.py). Kept local/offline on purpose: a config
# that has lost the key degrades to the no-account Parakeet provider rather than
# a cloud provider that needs a Google account (wh-stt-fallback-default-google).
DEFAULT_STT_PROVIDER = "parakeet_tdt"

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
        # Lets only one settings write run at a time; see save().
        self._save_lock = asyncio.Lock()
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

    def unset(self, key: str):
        """
        Removes a configuration key from memory, if it is there.

        Supports dot notation for nested keys (e.g., "stt.provider").

        This is what a caller needs to undo a set() for a key that was not
        there beforehand. Setting the key back to None reads as absent but
        cannot be written: tomli_w has no representation for it, so the next
        save fails and takes every unrelated setting down with it.

        Args:
            key: The configuration key to remove. Use dots for nested keys.
        """
        keys = key.split(".") if "." in key else [key]
        target = self._config
        for k in keys[:-1]:
            if not isinstance(target, dict) or k not in target:
                return
            target = target[k]
        if isinstance(target, dict):
            target.pop(keys[-1], None)

    async def save(self) -> bool:
        """
        Saves the current configuration to the TOML file.
        This is an async method that can be awaited.

        Returns True when the settings reached the disk and False when they did
        not. Callers act on a save: the floating button reports its new size, a
        provider switch logs success, a speech-mode change restarts the
        program. Reporting a failure as a success makes each of those act on
        settings the next start will not have.

        Two callers can reach this at once -- one gesture that changes two
        settings, or two settings changed close together -- and each write
        replaces the whole file. Two of them running at the same time can
        leave the file torn, so a lock lets only one write run at a time, and
        the write itself goes to a temporary file that replaces the real one
        only once it is complete. A crash or a failed write then leaves the
        previous settings intact rather than a half-written file.

        The lock alone is not enough. Callers change the settings in memory
        first and ask for the write afterwards, so while one write runs in its
        worker thread another task can change a value and then wait its turn.
        A write that read the live settings would pick up half of that later
        change -- a new size with an old position, the very mismatch that
        sending them together prevents. So the write takes its own complete
        copy of the settings first, before anything can run in between, and
        records that copy. The later change is not lost; it reaches the file in
        its own write.
        """
        import copy
        import os
        import tempfile

        import tomli_w

        # Taken here, synchronously, before the first await: nothing else can
        # run between the caller's change and this copy.
        snapshot = copy.deepcopy(self._config)

        def do_save() -> bool:
            """Synchronous file write operation for TOML config.

            Runs in thread pool via asyncio.to_thread to avoid blocking.
            """
            directory = os.path.dirname(os.path.abspath(self.config_path)) or "."
            handle = None
            temp_path = None
            try:
                handle, temp_path = tempfile.mkstemp(
                    dir=directory, prefix=".config-", suffix=".tmp"
                )
                with os.fdopen(handle, "wb") as f:
                    handle = None
                    tomli_w.dump(snapshot, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, self.config_path)
                temp_path = None
                logger.info(f"Configuration saved to {self.config_path}")
                return True
            except Exception as e:
                logger.error(f"Failed to save configuration: {e}")
                return False
            finally:
                if handle is not None:
                    try:
                        os.close(handle)
                    except OSError:
                        pass
                if temp_path is not None and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass

        async with self._save_lock:
            # Run the synchronous file I/O in a separate thread
            return await asyncio.to_thread(do_save)

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
