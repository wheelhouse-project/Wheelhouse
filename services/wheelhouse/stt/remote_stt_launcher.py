"""Remote STT provider discovery and lifecycle management.

This module discovers, starts, stops, and monitors remote STT providers.
Providers are discovered by scanning the services/stt_providers/ directory for config.toml
files containing a [provider] section.

Key Features:
  - Provider discovery via config.toml [provider] sections
  - Start providers by launching their launcher.py subprocess
  - Stop providers by sending shutdown command via WebSocket
  - Check provider status via PID files
"""
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import psutil

try:
    import tomllib
except ImportError:
    import tomli as tomllib

if TYPE_CHECKING:
    from integrations.websocket_manager import WebSocketManager
    from typing import Callable

logger = logging.getLogger(__name__)

# Type alias for notification callback
NotifyCallback = "Callable[[str, str], None]"

# Default timeout for provider startup (seconds). GPU/Vulkan providers can
# take 60s+ on first load (kernel compilation, CUDA context init, model paging).
# Individual providers can override via [provider].startup_timeout_seconds.
DEFAULT_STARTUP_TIMEOUT = 90


class RemoteSTTLauncher:
    """Discovers and manages remote STT provider lifecycle.

    Attributes:
        services_dir: Path to the services directory containing providers.
        app_data_dir: Path to app data directory for PID files.
        ws_host: WebSocket host to pass to providers.
        ws_port: WebSocket port to pass to providers.
        _providers: Cached list of discovered providers.
        _ws_manager: WebSocket manager for sending commands to providers.
    """

    def __init__(
        self,
        services_dir: Optional[Path] = None,
        app_data_dir: Optional[Path] = None,
        ws_host: str = "localhost",
        ws_port: int = 0,
        wake_word_config: Optional[dict] = None,
    ):
        """Initialize RemoteSTTLauncher.

        Args:
            services_dir: Path to services directory. Defaults to project's services/.
            app_data_dir: Path to app data directory. Defaults to %APPDATA%/WheelHouse.
            ws_host: WebSocket host to pass to providers when starting.
            ws_port: WebSocket port to pass to providers when starting.
            wake_word_config: Wake word configuration dict with keys: enabled, keyword,
                sensitivity, mode, model_dir. Passed to STT providers as CLI args.
        """
        if services_dir is None:
            # Default: project_root/services/stt_providers/
            project_root = Path(__file__).parent.parent.parent.parent
            services_dir = project_root / "services" / "stt_providers"
        self.services_dir = Path(services_dir)

        if app_data_dir is None:
            # Default: %APPDATA%/WheelHouse (Windows) or ~/.wheelhouse (Linux)
            if sys.platform == "win32":
                app_data_dir = Path(os.environ.get("APPDATA", "")) / "WheelHouse"
            else:
                app_data_dir = Path.home() / ".wheelhouse"
        self.app_data_dir = Path(app_data_dir)
        self.app_data_dir.mkdir(parents=True, exist_ok=True)

        self.ws_host = ws_host
        self.ws_port = ws_port
        self.wake_word_config: dict = wake_word_config or {}
        self._providers: Optional[list[dict]] = None
        self._ws_manager: Optional["WebSocketManager"] = None
        self._notify_callback: Optional["NotifyCallback"] = None
        self._show_working_callback: Optional["Callable[[str], None]"] = None
        self._hide_working_callback: Optional["Callable[[], None]"] = None
        # Event signaled when provider sends "ready" notification via WebSocket
        # Initialized as set so is_starting returns False before any provider launch
        self._provider_ready_event: threading.Event = threading.Event()
        self._provider_ready_event.set()
        # Popen handles for spawned provider subprocesses, keyed by provider name.
        # Used by _monitor_startup (wh-v0q) to check liveness on ready-timeout and
        # suppress false-failure notifications when the subprocess is still alive
        # (slow cold-start path).
        self._subprocesses: dict[str, subprocess.Popen] = {}

    def get_providers(self) -> list[dict]:
        """Get cached list of providers, discovering if not yet cached.

        Unlike discover_providers(), this returns cached results and only
        scans the disk on first call or after invalidate_cache().

        Returns:
            List of provider info dicts.
        """
        if self._providers is None:
            self.discover_providers()
        return self._providers or []

    def invalidate_cache(self) -> None:
        """Clear the provider cache, forcing re-discovery on next get_providers()."""
        self._providers = None

    def set_websocket_manager(self, ws_manager: "WebSocketManager") -> None:
        """Set the WebSocket manager for sending commands to providers.

        Args:
            ws_manager: WebSocket manager instance.
        """
        self._ws_manager = ws_manager

    def set_notify_callback(self, callback: "NotifyCallback") -> None:
        """Set callback for sending user notifications.

        Args:
            callback: Function taking (title, message) to send notifications.
        """
        self._notify_callback = callback

    def _notify(self, title: str, message: str) -> None:
        """Send a notification if callback is set.

        Args:
            title: Notification title.
            message: Notification message.
        """
        if self._notify_callback:
            try:
                self._notify_callback(title, message)
            except Exception as e:
                logger.warning(f"Failed to send notification: {e}")

    def set_working_callback(self, show: "Callable[[str], None]", hide: "Callable[[], None]") -> None:
        """Set callbacks for showing/hiding the working dialog.

        Args:
            show: Function taking a message string to show the working dialog.
            hide: Function to hide the working dialog.
        """
        self._show_working_callback = show
        self._hide_working_callback = hide

    def _show_working(self, message: str) -> None:
        """Show working dialog if callback is set."""
        if self._show_working_callback:
            try:
                self._show_working_callback(message)
            except Exception as e:
                logger.warning(f"Failed to show working dialog: {e}")

    def _hide_working(self) -> None:
        """Hide working dialog if callback is set."""
        if self._hide_working_callback:
            try:
                self._hide_working_callback()
            except Exception as e:
                logger.warning(f"Failed to hide working dialog: {e}")

    def _resolve_display_name(self, display_name: str, service_dir: Path) -> str:
        """Resolve {mode} placeholder in display_name using provider config.

        Reads the provider's config.toml to determine CPU/GPU mode.
        """
        try:
            config_path = service_dir / "config.toml"
            if config_path.exists():
                import tomllib
                with open(config_path, "rb") as f:
                    config = tomllib.load(f)
                use_gpu = config.get("model", {}).get("use_gpu", False)
                return display_name.replace("{mode}", "GPU" if use_gpu else "CPU")
        except Exception as e:
            logger.warning(f"Failed to resolve display name placeholder: {e}")
        return display_name.replace("{mode}", "CPU")

    def signal_provider_ready(self) -> None:
        """Signal that the provider has sent its 'ready' notification.

        Called by WebSocketManager when it receives a notification with
        'ready' or 'service ready' in the message. This cancels the
        startup timeout monitor.
        """
        self._provider_ready_event.set()
        logger.debug("Provider ready signal received")

    @property
    def is_starting(self) -> bool:
        """True while a provider startup is in progress (before 'ready' received)."""
        return not self._provider_ready_event.is_set()

    def _monitor_startup(
        self,
        provider_name: str,
        display_name: str,
        timeout: float = DEFAULT_STARTUP_TIMEOUT,
    ) -> None:
        """Monitor provider startup in background thread.

        Waits for either:
        1. The provider to send a 'ready' notification via WebSocket
        2. The timeout to expire

        If timeout expires without receiving ready signal, sends failure notification.

        Args:
            provider_name: Internal provider name for PID file lookup.
            display_name: User-friendly name for notifications.
            timeout: Maximum seconds to wait for startup.
        """
        # Clear the event before waiting (in case it was set from a previous startup)
        self._provider_ready_event.clear()

        # Wait for the ready signal with timeout
        ready = self._provider_ready_event.wait(timeout=timeout)

        if ready:
            logger.debug(f"Provider {provider_name} startup confirmed via ready notification")
            # No failure notification needed - provider already sent "ready" toast
            return

        # Timeout reached without ready signal. Before crying wolf, check whether
        # the subprocess we spawned is still alive -- GPU cold-start can easily
        # exceed the ready-signal timeout while the provider is perfectly healthy
        # and will transcribe correctly shortly after (wh-v0q).
        proc = self._subprocesses.get(provider_name)
        subprocess_alive = proc is not None and proc.poll() is None

        if subprocess_alive:
            logger.warning(
                f"Provider {provider_name} did not send ready notification within {timeout}s, "
                f"but subprocess is still alive. Suppressing failure notification -- "
                f"provider is likely still warming up (wh-v0q)."
            )
            # Hide the working dialog so the UI doesn't hang forever. The provider
            # will still send its own ready/error notifications when it finishes
            # warming up.
            self._hide_working()
        else:
            logger.warning(
                f"Provider {provider_name} did not send ready notification within {timeout}s "
                f"and subprocess is not alive."
            )
            self._hide_working()
            self._notify(display_name, "Failed to start - try restarting WheelHouse")

    def discover_providers(self) -> list[dict]:
        """Scan services directory for STT providers.

        Providers are identified by having a config.toml file with a
        [provider] section containing name, display_name, and launcher fields.

        Returns:
            List of provider info dicts with keys: name, display_name, launcher, service_dir
        """
        providers = []

        if not self.services_dir.exists():
            logger.warning(f"Services directory not found: {self.services_dir}")
            return providers

        for service_dir in self.services_dir.iterdir():
            if not service_dir.is_dir():
                continue

            config_path = service_dir / "config.toml"
            if not config_path.exists():
                continue

            try:
                with open(config_path, "rb") as f:
                    config = tomllib.load(f)

                # Check for [provider] section
                provider_section = config.get("provider")
                if not provider_section:
                    continue

                # Extract required fields
                name = provider_section.get("name")
                display_name = provider_section.get("display_name")
                launcher = provider_section.get("launcher")

                if not all([name, display_name, launcher]):
                    logger.warning(
                        f"Incomplete [provider] section in {config_path}: "
                        f"name={name}, display_name={display_name}, launcher={launcher}"
                    )
                    continue

                # Skip disabled providers
                if not provider_section.get("enabled", True):
                    logger.debug(f"Skipping disabled provider: {name}")
                    continue

                # Skip templates (not actual providers)
                if provider_section.get("template", False):
                    logger.debug(f"Skipping template provider: {name}")
                    continue

                # Verify launcher exists
                launcher_path = service_dir / launcher
                if not launcher_path.exists():
                    logger.warning(
                        f"Launcher not found for provider {name}: {launcher_path}"
                    )
                    continue

                # Optional per-provider startup timeout override (wh-v0q).
                # Falls back to DEFAULT_STARTUP_TIMEOUT if unset or invalid.
                startup_timeout = provider_section.get("startup_timeout_seconds")
                if not isinstance(startup_timeout, (int, float)) or startup_timeout <= 0:
                    startup_timeout = DEFAULT_STARTUP_TIMEOUT

                # Resolve {mode} placeholder up-front so the tray menu and
                # every downstream consumer see the same CPU/GPU-resolved
                # string (the menu populates from discovery, not from
                # start_provider() which was previously the only call site).
                if "{mode}" in display_name:
                    display_name = self._resolve_display_name(display_name, service_dir)

                providers.append({
                    "name": name,
                    "display_name": display_name,
                    "launcher": launcher,
                    "service_dir": service_dir,
                    "startup_timeout_seconds": float(startup_timeout),
                })
                logger.debug(f"Discovered provider: {name} at {service_dir}")

            except Exception as e:
                logger.error(f"Error reading config from {config_path}: {e}")
                continue

        self._providers = providers
        logger.info(f"Discovered {len(providers)} STT providers")
        return providers

    def get_provider_by_name(self, name: str) -> Optional[dict]:
        """Get provider info by name.

        Args:
            name: Provider name (e.g., "google_stt", "zipformer").

        Returns:
            Provider info dict, or None if not found.
        """
        if self._providers is None:
            self.discover_providers()

        for provider in self._providers:
            if provider["name"] == name:
                return provider
        return None

    def _get_pid_file_path(self, provider_name: str) -> Path:
        """Get the PID file path for a provider.

        Args:
            provider_name: Provider name.

        Returns:
            Path to the PID file.
        """
        return self.app_data_dir / f"{provider_name}.pid"

    def is_running(self, provider_name: str) -> bool:
        """Check if a provider is currently running.

        Checks if the PID file exists and the process is alive.
        Cleans up stale PID files if process is dead.

        Args:
            provider_name: Provider name.

        Returns:
            True if provider is running, False otherwise.
        """
        pid_file = self._get_pid_file_path(provider_name)

        if not pid_file.exists():
            return False

        try:
            pid = int(pid_file.read_text().strip())
            if psutil.pid_exists(pid):
                return True
            else:
                # Stale PID file - process is dead
                logger.debug(f"Removing stale PID file for {provider_name}")
                pid_file.unlink()
                # Also clean up stale port file
                port_file = self.app_data_dir / f"{provider_name}.port"
                try:
                    port_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return False
        except (ValueError, OSError) as e:
            logger.warning(f"Error reading PID file for {provider_name}: {e}")
            try:
                pid_file.unlink()
            except OSError:
                pass
            return False

    def _terminate_stale_provider(self, provider_name: str) -> None:
        """Terminate a stale provider process from a previous WheelHouse session.

        Args:
            provider_name: Provider name (for PID/port file lookup).
        """
        pid_file = self._get_pid_file_path(provider_name)
        port_file = self.app_data_dir / f"{provider_name}.port"

        try:
            pid = int(pid_file.read_text().strip())
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
            logger.info(f"Terminated stale provider {provider_name} (PID {pid})")
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, OSError) as e:
            logger.debug(f"Could not terminate stale provider {provider_name}: {e}")

        # Clean up files
        for f in (pid_file, port_file):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    def start_provider(self, provider_name: str) -> bool:
        """Start a provider by launching its launcher.py subprocess.

        If the provider is already running, returns True without starting.

        Args:
            provider_name: Provider name.

        Returns:
            True if provider started or already running, False on error.
        """
        provider = self.get_provider_by_name(provider_name)
        if provider is None:
            logger.error(f"Unknown provider: {provider_name}")
            return False

        # Guard: port must be assigned before starting any provider
        if self.ws_port == 0:
            logger.error("Cannot start provider: WebSocket port not assigned yet")
            return False

        # Check if already running with correct port
        if self.is_running(provider_name):
            port_file = self.app_data_dir / f"{provider_name}.port"
            try:
                stored_port = int(port_file.read_text().strip()) if port_file.exists() else None
            except (ValueError, OSError):
                stored_port = None

            if stored_port == self.ws_port:
                logger.info(f"Provider {provider_name} is already running on port {self.ws_port}")
                return True
            else:
                # Port mismatch or missing - old provider from previous session
                logger.warning(
                    f"Provider {provider_name} running on port {stored_port}, "
                    f"but current port is {self.ws_port} - terminating stale process"
                )
                self._terminate_stale_provider(provider_name)

        service_dir = provider["service_dir"]
        launcher_script = service_dir / provider["launcher"]
        display_name = provider.get("display_name", provider_name)

        # Resolve {mode} placeholder for providers with CPU/GPU variants
        if "{mode}" in display_name:
            display_name = self._resolve_display_name(display_name, service_dir)

        # Clean up any stale PID file before starting
        # This ensures the monitor thread only sees a fresh PID file from this launch
        pid_file = self._get_pid_file_path(provider_name)
        if pid_file.exists():
            try:
                pid_file.unlink()
                logger.debug(f"Removed old PID file before starting {provider_name}")
            except OSError as e:
                logger.warning(f"Failed to remove old PID file: {e}")

        # Show working dialog immediately before starting
        self._show_working(f"Loading {display_name}")

        try:
            logger.info(f"Starting provider {provider_name} from {launcher_script}")
            # Start the launcher subprocess via uv to ensure correct virtualenv.
            # Each provider has its own uv project with dependencies; --directory
            # tells uv which project to resolve. Clear VIRTUAL_ENV so uv does not
            # inherit the parent process's virtualenv.
            #
            # --locked --no-sync: bootstrap has already synced each service venv,
            # so runtime launch must not sync or relock. Otherwise every provider
            # switch could hit the network, mutate venv state mid-run, or block
            # past the startup-monitor deadline. --locked also fails loudly if
            # the lockfile is out of date with pyproject.toml.
            env = os.environ.copy()
            env.pop("VIRTUAL_ENV", None)
            cmd = [
                "uv", "run", "--directory", str(service_dir),
                "--locked", "--no-sync",
                "python", str(launcher_script),
                "--ws-host", self.ws_host,
                "--ws-port", str(self.ws_port),
            ]

            # Append wake word config as CLI args if enabled
            if self.wake_word_config.get("enabled", False):
                ww = self.wake_word_config
                cmd.extend(["--wake-word-enabled"])
                if "keyword" in ww:
                    cmd.extend(["--wake-word-keyword", str(ww["keyword"])])
                if "sensitivity" in ww:
                    cmd.extend(["--wake-word-sensitivity", str(ww["sensitivity"])])
                if "mode" in ww:
                    cmd.extend(["--wake-word-mode", str(ww["mode"])])
                if "model_dir" in ww:
                    model_dir = self._resolve_wake_word_model_dir(
                        str(ww["model_dir"]),
                        service_dir,
                    )
                    cmd.extend(["--wake-word-model-dir", str(model_dir)])

            proc = subprocess.Popen(
                cmd,
                cwd=str(service_dir),
                env=env,
                # Detach from parent process
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                if sys.platform == "win32" else 0,
                start_new_session=True if sys.platform != "win32" else False,
            )
            # Track subprocess for liveness checks in _monitor_startup (wh-v0q)
            self._subprocesses[provider_name] = proc
            logger.info(f"Provider {provider_name} launcher started with --ws-host={self.ws_host} --ws-port={self.ws_port}")

            # Write port file so we can detect stale providers after restart
            port_file = self.app_data_dir / f"{provider_name}.port"
            try:
                port_file.write_text(str(self.ws_port))
            except OSError as e:
                logger.warning(f"Failed to write port file: {e}")

            # Start background thread to monitor startup and notify on timeout.
            # Use the per-provider startup_timeout_seconds from discovery (wh-v0q).
            provider_timeout = float(provider.get("startup_timeout_seconds", DEFAULT_STARTUP_TIMEOUT))
            monitor_thread = threading.Thread(
                target=self._monitor_startup,
                args=(provider_name, display_name, provider_timeout),
                daemon=True,
            )
            monitor_thread.start()

            return True

        except Exception as e:
            logger.error(f"Failed to start provider {provider_name}: {e}")
            self._hide_working()
            self._notify(display_name, "Failed to start - try restarting WheelHouse")
            return False

    def _resolve_wake_word_model_dir(self, model_dir_value: str, service_dir: Path) -> Path:
        """Resolve wake-word model directory for provider startup.

        Resolution order:
        1. Absolute path as-is
        2. Relative to provider service dir
        3. Relative to stt_providers root
        4. Legacy fallback: shared/<model_dir> (for default data/wake_words)
        5. Provider-relative path (even if missing, so provider can create/download)
        """
        model_dir = Path(model_dir_value)
        if model_dir.is_absolute():
            return model_dir

        provider_relative = (service_dir / model_dir).resolve()
        if provider_relative.exists():
            return provider_relative

        providers_relative = (self.services_dir / model_dir).resolve()
        if providers_relative.exists():
            return providers_relative

        shared_fallback = (self.services_dir / "shared" / model_dir).resolve()
        if shared_fallback.exists():
            logger.info(
                "Wake-word model_dir '%s' not found under %s, using shared fallback %s",
                model_dir_value,
                service_dir,
                shared_fallback,
            )
            return shared_fallback

        return provider_relative

    async def stop_provider(self, provider_name: str) -> bool:
        """Stop a provider by sending shutdown command.

        Sends shutdown command via WebSocket. The provider will exit cleanly
        and the launcher will not restart it (exit code 0).

        Args:
            provider_name: Provider name.

        Returns:
            True if shutdown sent or provider not running, False on error.
        """
        provider = self.get_provider_by_name(provider_name)
        if provider is None:
            logger.error(f"Unknown provider: {provider_name}")
            return False

        # Check if running
        if not self.is_running(provider_name):
            logger.info(f"Provider {provider_name} is not running")
            return True

        if self._ws_manager is None:
            # No WebSocket connection means provider isn't connected - treat as stopped
            logger.debug("WebSocket manager not set - provider not connected, skipping shutdown")
            return True

        try:
            logger.info(f"Sending shutdown command to {provider_name}")
            await self._ws_manager.send_command_to_stt("shutdown")
            return True

        except Exception as e:
            logger.error(f"Failed to send shutdown to {provider_name}: {e}")
            return False

    async def shutdown_all_providers(self) -> dict[str, bool]:
        """Shutdown all discovered providers.

        Sends shutdown command to each running provider. Non-running providers
        are skipped (returns True for them). Continues even if individual
        providers fail to shutdown.

        Returns:
            Dict mapping provider name to shutdown success (True/False).
        """
        if self._providers is None:
            self.discover_providers()

        if not self._providers:
            logger.info("No providers to shutdown")
            return {}

        results = {}
        for provider in self._providers:
            name = provider["name"]
            try:
                result = await self.stop_provider(name)
                results[name] = result
            except Exception as e:
                logger.error(f"Error shutting down {name}: {e}")
                results[name] = False

        return results
