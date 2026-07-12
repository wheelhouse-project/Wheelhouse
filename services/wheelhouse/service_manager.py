"""Service lifecycle management and dependency injection for WheelHouse.

This module implements the ServiceManager class, which acts as the central
dependency injection container and service lifecycle coordinator for all
WheelHouse services. It follows explicit dependency injection patterns to
manage service creation, initialization, and shutdown in a clean, testable way.

Key Classes:
  - ServiceManager: Main service container and lifecycle manager.

Key Responsibilities:
  - Service instantiation with explicit dependency injection
  - Service lifecycle management (start, stop, restart)
  - Configuration distribution to services
  - Service health monitoring and error handling
  - Clean shutdown coordination

Services Managed:
  - MouseHandler: High-resolution mouse input processing
  - AudioMonitor: System audio level monitoring
  - SpeechHandler: Voice command processing pipeline
  - BraviaControl: Sony Bravia TV integration
  - SoftwareDimmer: Display dimming control
  - PluginRegistry: Plugin system for modular integrations (Sonos, Bravia, WindowPositioning, etc.)

Typical Usage:
  from service_manager import ServiceManager
  from config_service import ConfigService
  from event_bus import EventBus
  
  config_service = ConfigService()
  event_bus = EventBus()
  service_mgr = ServiceManager(config_service, event_bus, loop, app, state_manager)
  service_mgr.set_logic_controller(logic_controller)
  
  # Services are accessed via properties
  mouse_handler = service_mgr.mouse_handler
  speech_handler = service_mgr.speech_handler
"""
import asyncio
import logging
from typing import Optional, Any, Dict, TYPE_CHECKING

from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.handlers.mouse_handler import MouseHandler
from services.wheelhouse.config_service import ConfigService
from services.wheelhouse.utils.screen import get_screen_size
from services.wheelhouse.integrations.bravia_control import BraviaControl
from services.wheelhouse.handlers.software_dimmer import SoftwareDimmer
from services.wheelhouse.handlers.audio_monitor import AudioMonitor
from services.wheelhouse.speech.speech_handler import SpeechHandler
from services.wheelhouse.plugins.registry import PluginRegistry
from services.wheelhouse.coordinators.brightness_coordinator import BrightnessCoordinator
from services.wheelhouse.stt.remote_stt_launcher import RemoteSTTLauncher
from services.wheelhouse.ai.service import AIService


if TYPE_CHECKING:
    from services.wheelhouse.main import LogicController
    from services.wheelhouse.state_manager import StateManager
    from services.wheelhouse.app import WheelHouseApp


log = logging.getLogger(__name__)

class ServiceManager:
    """
    Manages the lifecycle of all hardware and software services.

    This class acts as the central "dependency assembler" for the application. It
    is responsible for instantiating all service classes (e.g., MouseHandler,
    AudioMonitor) and injecting their required dependencies explicitly via their
    constructors.

    This approach follows the "Explicit Dependency Injection" pattern. For more
    details on this architectural principle, see `docs/system_knowledge.md`.
    """

    def __init__(self, config_service: "ConfigService", event_bus: "EventBus", loop: asyncio.AbstractEventLoop, app: "WheelHouseApp", state_manager: "StateManager"):
        self.config_service = config_service
        self.event_bus = event_bus
        self.loop = loop
        self.app = app
        self.state_manager = state_manager
        self.logic_controller: Optional['LogicController'] = None

        # Retrieve config from the service
        self.config = self.config_service.get_config()

        # Get screen dimensions
        self.screen_width, self.screen_height = self.app.get_screen_dimensions()

        # Service instances
        self.bravia_control: Optional[BraviaControl] = None
        self.software_dimmer: Optional[Any] = None  # SoftwareDimmer or GammaDimmer
        self.audio_monitor: Optional[AudioMonitor] = None
        self.mouse_handler: Optional[MouseHandler] = None
        self.speech_handler: Optional[SpeechHandler] = None
        self.stt_manager: Optional[Any] = None  # STTManager for in-process STT
        
        # Coordinators
        self.brightness_coordinator: Optional[BrightnessCoordinator] = None

        # Plugin system
        self.plugin_registry: Optional[PluginRegistry] = None

        # Remote STT provider launcher (for remote mode)
        self.remote_stt_launcher: Optional[RemoteSTTLauncher] = None

        # AI Service (text correction, help Q&A)
        self.ai_service: Optional[AIService] = None

    def set_logic_controller(self, logic_controller: 'LogicController'):
        """Sets the logic controller after initialization to break circular dependency."""
        self.logic_controller = logic_controller

    def initialize_services(self):
        """:flow: Application Lifecycle
        :step: 4
        :consumes_from: Application Lifecycle
        :produces_for: Application Lifecycle
        :description: Instantiates all service objects with dependency injection
        :data_in: ConfigService, EventBus, App, StateManager
        :data_out: Initialized service instances (MouseHandler, AudioMonitor, etc.)
        :notes: Creates all service instances but does not start their background tasks yet. Injects dependencies explicitly. Initializes the PluginRegistry for modular extensions. This prepares the system for startup (Step 5).
        """
        """Initializes all service instances."""
        log.info("Initializing services...")
        self.bravia_control = BraviaControl(
            self.config_service.get("BRAVIA_IP", ""), 
            self.config_service.get("BRAVIA_PSK", "")
        )
        # Create software dimmer based on config (gamma_dimmer | software_dimmer | flux)
        dimmer_type = self.config_service.get("brightness_coordinator.software_dimmer", "flux")
        if dimmer_type == "gamma_dimmer":
            from services.wheelhouse.handlers.gamma_dimmer import GammaDimmer
            self.software_dimmer = GammaDimmer(self.loop)
            log.info("Using GammaDimmer (native gamma ramp)")
        elif dimmer_type in ("software_dimmer", "overlay"):
            self.software_dimmer = SoftwareDimmer(self.loop)
            log.info("Using SoftwareDimmer (overlay window)")
        else:  # flux or other
            self.software_dimmer = None
            log.info(f"Using {dimmer_type} (external control via hotkeys)")
        self.audio_monitor = AudioMonitor(self.loop, self.config_service, self.event_bus)
        
        # Initialize brightness coordinator (before MouseHandler, after EventBus and SoftwareDimmer)
        log.info("Initializing brightness coordinator...")
        self.brightness_coordinator = BrightnessCoordinator(
            self.config_service,
            self.event_bus,
            self.software_dimmer
        )
        
        self.mouse_handler = MouseHandler(self.loop, self.config_service, self.app, self.audio_monitor, self.bravia_control, self.software_dimmer, self.event_bus)
        self.speech_handler = SpeechHandler(self.app, self.logic_controller, self.config_service)
        
        # Initialize plugin system
        log.info("Initializing plugin system...")
        self.plugin_registry = PluginRegistry(self.config_service, self.event_bus)
        
        # Initialize STT based on mode
        stt_mode = self.config_service.get("stt.mode", "remote")
        if stt_mode == "in_process":
            try:
                from stt.stt_manager import STTManager

                # Read VAD configuration (enabled by default for in-process STT)
                vad_enabled = self.config_service.get("stt.vad.enabled", True)
                vad_threshold = self.config_service.get("stt.vad.threshold", 0.5)
                vad_lead_in_chunks = self.config_service.get("stt.vad.lead_in_chunks", 3)

                self.stt_manager = STTManager(
                    vad_enabled=vad_enabled,
                    vad_threshold=vad_threshold,
                    vad_lead_in_chunks=vad_lead_in_chunks,
                )
                self._stt_provider_type = self.config_service.get("stt.provider", "google")
                self._stt_provider_kwargs = self._build_stt_provider_kwargs(self._stt_provider_type)
                log.info(f"In-process STT enabled, provider: {self._stt_provider_type}, VAD: {vad_enabled}")
                self.state_manager.set_stt_manager(self.stt_manager)
            except ImportError as e:
                log.warning(f"Could not initialize STTManager: {e}")
        else:
            # Remote mode - initialize RemoteSTTLauncher for provider discovery/management
            log.info("Using remote STT mode (WebSocket to external STT server)")
            self.remote_stt_launcher = self._build_remote_stt_launcher()
            providers = self.remote_stt_launcher.discover_providers()
            log.info(f"Discovered {len(providers)} remote STT providers: {[p['name'] for p in providers]}")
            # Wire launcher to state_manager for provider discovery in GUI
            self.state_manager.set_remote_stt_launcher(self.remote_stt_launcher)
        
        # Initialize AI Service if enabled
        if self.config_service.get("ai.enabled", False):
            self.ai_service = AIService(
                config_service=self.config_service,
            )
            self.state_manager.set_ai_service(self.ai_service)
            log.info("AIService initialized")

        log.info("Services initialized.")

    def _build_remote_stt_launcher(self, app_data_dir=None) -> RemoteSTTLauncher:
        """Construct the RemoteSTTLauncher from config.

        stt.ws_host (default "localhost") is the address the spawned STT
        provider processes are told to connect back to
        (wh-stt-client-address) -- the STT-side counterpart of the AI
        client's configured server address. It is distinct from the
        legacy SPEECH_WEBSOCKET_HOST key, which sets the address the
        Logic WebSocket server BINDS to; set both when running STT on
        another machine (bind all interfaces, ws_host = this machine's LAN
        address). The port is not configured here: the server takes an
        OS-assigned port and main.py copies it onto the launcher after
        startup.

        app_data_dir is a test seam; production passes None and the
        launcher resolves its own default.
        """
        wake_word_config = {
            "enabled": self.config_service.get("wake_word.enabled", False),
            "keyword": self.config_service.get("wake_word.keyword", "computer"),
            "sensitivity": self.config_service.get("wake_word.sensitivity", 0.5),
            "mode": self.config_service.get("wake_word.mode", "idle_recovery"),
            "model_dir": self.config_service.get("wake_word.model_dir", "../shared/data/wake_words"),
        }
        return RemoteSTTLauncher(
            app_data_dir=app_data_dir,
            ws_host=self.config_service.get("stt.ws_host", "localhost"),
            wake_word_config=wake_word_config,
        )

    def start_services(self):
        """:flow: Application Lifecycle
        :step: 5
        :consumes_from: Application Lifecycle
        :description: Starts long-running background tasks for all services
        :data_in: Initialized service instances
        :data_out: List of asyncio.Task objects
        :notes: Calls .start() on each service (MouseHandler, AudioMonitor, etc.) to launch their background loops. Also starts the PluginRegistry to load and start plugins. Returns the list of tasks to the LogicController for monitoring.
        """
        """Starts all long-running services and returns their tasks."""
        tasks = []
        if self.software_dimmer:
            self.software_dimmer.start()
        
        # Start brightness coordinator (before plugins)
        if self.brightness_coordinator:
            self.brightness_coordinator.start()
            log.info("BrightnessCoordinator started")
        
        if self.mouse_handler:
            tasks.extend(self.mouse_handler.start())
        if self.audio_monitor:
            tasks.append(self.audio_monitor.start())
        
        # Start plugin system
        if self.plugin_registry:
            tasks.append(self.loop.create_task(self._start_plugins()))

        # Start AI service (async -- needs provider availability check)
        if self.ai_service:
            tasks.append(self.loop.create_task(self._start_ai_service()))

        return tasks
    
    async def _start_plugins(self):
        """Discover, initialize, and start all plugins."""
        try:
            if not self.plugin_registry:
                return
            log.info("Starting plugin system...")
            await self.plugin_registry.discover_plugins()
            await self.plugin_registry.initialize_all()
            await self.plugin_registry.start_all()
            log.info("Plugin system started successfully")
        except Exception as e:
            log.error(f"Error starting plugin system: {e}", exc_info=True)

    async def _start_ai_service(self):
        """Start AIService (provider check, knowledge base load)."""
        try:
            if not self.ai_service:
                return
            await self.ai_service.start()
            log.info("AIService started successfully")
        except Exception as e:
            log.error(f"Error starting AIService: {e}", exc_info=True)

    def _build_stt_provider_kwargs(self, provider_type: str) -> dict:
        """Build provider-specific kwargs from config.

        Args:
            provider_type: The STT provider type (google, azure).

        Returns:
            Dictionary of kwargs for the provider constructor.
        """
        if provider_type == "google":
            return {
                "language": self.config_service.get("stt.google.language", "en-US"),
                "boost_words": self.config_service.get("stt.google.boost_words", []),
            }
        elif provider_type == "azure":
            return {
                "subscription_key": self.config_service.get("stt.azure.subscription_key", ""),
                "region": self.config_service.get("stt.azure.region", "eastus"),
            }
        return {}

    async def start_stt_manager(self, transcript_handler) -> None:
        """Start the in-process STTManager with transcript routing.
        
        :flow: In-Process STT
        :step: 1
        :description: Starts STTManager with configured provider and wires transcript handler
        :data_in: transcript_handler callback, provider config from config.toml
        :data_out: Running STTManager with audio capture and transcription
        :notes: Called from main.py after services initialize. Registers transcript 
                callback to route TranscriptEvents to speech processing pipeline.
        
        Args:
            transcript_handler: Async callback for TranscriptEvent objects.
        """
        if not self.stt_manager:
            return
        
        # Register transcript handler
        self.stt_manager.on_transcript(transcript_handler)
        
        # Start with configured provider
        await self.stt_manager.start(
            self._stt_provider_type,
            **self._stt_provider_kwargs,
        )
        log.info(f"STTManager started with {self._stt_provider_type}")

    def start_remote_stt(self) -> bool:
        """Start the remote STT provider based on last_provider config.

        Attempts to start the provider specified in stt.last_provider config.
        If that provider is not found or fails to start, falls back to the
        first available provider.

        Returns:
            True if a provider was started (or already running), False otherwise.
        """
        if not self.remote_stt_launcher:
            log.warning("RemoteSTTLauncher not initialized - cannot start remote STT")
            return False

        # Get last selected provider from config
        last_provider = self.config_service.get("stt.last_provider", None)
        providers = self.remote_stt_launcher.discover_providers()

        if not providers:
            log.warning("No remote STT providers discovered")
            return False

        # Try to start last selected provider
        if last_provider:
            if self.remote_stt_launcher.start_provider(last_provider):
                log.info(f"Started remote STT provider: {last_provider}")
                return True
            else:
                log.warning(f"Failed to start last provider '{last_provider}', falling back to first available")

        # Fall back to first available provider
        first_provider = providers[0]["name"]
        if self.remote_stt_launcher.start_provider(first_provider):
            log.info(f"Started remote STT provider (fallback): {first_provider}")
            return True

        log.error("Failed to start any remote STT provider")
        return False

    async def shutdown_services(self):
        """:flow: Application Lifecycle
        :step: 7
        :consumes_from: Application Lifecycle
        :description: Stops all services and plugins gracefully
        :data_in: None
        :data_out: Stopped services
        :notes: Iterates through all services and calls their stop methods. Stops remote STT providers first, then plugins, then core services. Ensures hardware resources (like audio streams) are released and background tasks are cancelled.
        """
        """Stops all services gracefully."""
        log.info("Shutting down services...")

        # Shutdown remote STT providers first (T7: Shutdown on exit)
        if self.remote_stt_launcher:
            try:
                results = await self.remote_stt_launcher.shutdown_all_providers()
                log.info(f"Remote STT provider shutdown results: {results}")
            except Exception as e:
                log.error(f"Error shutting down remote STT providers: {e}", exc_info=True)

        # Stop plugins first
        if self.plugin_registry:
            try:
                await self.plugin_registry.stop_all()
            except Exception as e:
                log.error(f"Error stopping plugins: {e}", exc_info=True)
        
        # Stop STTManager
        if self.stt_manager:
            try:
                await self.stt_manager.stop()
                log.info("STTManager stopped")
            except Exception as e:
                log.error(f"Error stopping STTManager: {e}", exc_info=True)
        
        # Stop brightness coordinator
        if self.brightness_coordinator:
            try:
                self.brightness_coordinator.stop()
                log.info("BrightnessCoordinator stopped")
            except Exception as e:
                log.error(f"Error stopping BrightnessCoordinator: {e}", exc_info=True)
        
        # Stop AI service
        if self.ai_service:
            try:
                await self.ai_service.stop()
                log.info("AIService stopped")
            except Exception as e:
                log.error(f"Error stopping AIService: {e}", exc_info=True)

        if self.mouse_handler:
            await asyncio.to_thread(self.mouse_handler.stop_listeners)
        if self.software_dimmer:
            await asyncio.to_thread(self.software_dimmer.stop)
        log.info("Services shut down.")
