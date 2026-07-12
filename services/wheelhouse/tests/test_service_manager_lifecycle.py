"""Tests for ServiceManager lifecycle (P1-T5).

Extends the existing shutdown tests (test_service_manager_shutdown.py) with
lifecycle coverage: construction, initialize_services, start_services,
start_remote_stt, start_stt_manager, and _build_stt_provider_kwargs.

Key behaviors tested:
- Constructor stores all dependencies
- set_logic_controller breaks circular dependency
- initialize_services creates all service instances
- initialize_services handles dimmer type config variants
- initialize_services handles in_process vs remote STT modes
- start_services starts all services and returns tasks
- start_remote_stt provider discovery and fallback logic
- _build_stt_provider_kwargs for google/azure/unknown providers
- start_stt_manager wires transcript handler and starts STT
"""
import asyncio
from unittest.mock import Mock, AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_deps():
    """Create mock dependencies for ServiceManager constructor."""
    config_data = {}

    config_service = MagicMock()
    config_service.get.return_value = None
    config_service.get_config.return_value = config_data

    event_bus = MagicMock()
    loop = asyncio.new_event_loop()

    app = MagicMock()
    app.get_screen_dimensions.return_value = (1920, 1080)

    state_manager = MagicMock()

    return {
        "config_service": config_service,
        "event_bus": event_bus,
        "loop": loop,
        "app": app,
        "state_manager": state_manager,
    }


@pytest.fixture
def service_manager(mock_deps):
    """Create a ServiceManager instance with mocked dependencies."""
    from service_manager import ServiceManager
    return ServiceManager(**mock_deps)


def _config_get_side_effect(overrides=None):
    """Create a config.get side_effect function with optional overrides.

    Returns a function suitable for config_service.get.side_effect that
    first checks overrides dict, then returns the default parameter.
    """
    overrides = overrides or {}

    def _get(key, default=None):
        if key in overrides:
            return overrides[key]
        return default

    return _get


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------

class TestConstructor:
    """Test ServiceManager.__init__."""

    def test_stores_dependencies(self, service_manager, mock_deps):
        """Constructor stores all injected dependencies."""
        assert service_manager.config_service is mock_deps["config_service"]
        assert service_manager.event_bus is mock_deps["event_bus"]
        assert service_manager.loop is mock_deps["loop"]
        assert service_manager.app is mock_deps["app"]
        assert service_manager.state_manager is mock_deps["state_manager"]

    def test_services_initially_none(self, service_manager):
        """All service instances are None before initialize_services."""
        assert service_manager.bravia_control is None
        assert service_manager.software_dimmer is None
        assert service_manager.audio_monitor is None
        assert service_manager.mouse_handler is None
        assert service_manager.speech_handler is None
        assert service_manager.stt_manager is None
        assert service_manager.plugin_registry is None
        assert service_manager.remote_stt_launcher is None

    def test_logic_controller_initially_none(self, service_manager):
        """Logic controller is None until set_logic_controller called."""
        assert service_manager.logic_controller is None

    def test_reads_config_dict(self, service_manager, mock_deps):
        """Constructor calls get_config() to cache config dict."""
        mock_deps["config_service"].get_config.assert_called_once()

    def test_reads_screen_dimensions(self, service_manager, mock_deps):
        """Constructor calls app.get_screen_dimensions()."""
        mock_deps["app"].get_screen_dimensions.assert_called_once()
        assert service_manager.screen_width == 1920
        assert service_manager.screen_height == 1080


# ---------------------------------------------------------------------------
# set_logic_controller
# ---------------------------------------------------------------------------

class TestSetLogicController:
    """Test set_logic_controller circular dependency breaker."""

    def test_stores_logic_controller(self, service_manager):
        """set_logic_controller stores the reference."""
        mock_lc = MagicMock()
        service_manager.set_logic_controller(mock_lc)
        assert service_manager.logic_controller is mock_lc


# ---------------------------------------------------------------------------
# initialize_services
# ---------------------------------------------------------------------------

class TestInitializeServices:
    """Test initialize_services creates all service instances."""

    @patch("service_manager.BraviaControl")
    @patch("service_manager.AudioMonitor")
    @patch("service_manager.MouseHandler")
    @patch("service_manager.SpeechHandler")
    @patch("service_manager.PluginRegistry")
    @patch("service_manager.RemoteSTTLauncher")
    def test_creates_bravia_control(
        self, mock_launcher_cls, mock_plugin_cls, mock_speech_cls,
        mock_mouse_cls, mock_audio_cls, mock_bravia_cls, service_manager
    ):
        """initialize_services creates BraviaControl with config values."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "BRAVIA_IP": "192.168.1.10",
            "BRAVIA_PSK": "secret",
            "stt.mode": "remote",
        })
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = []
        mock_launcher_cls.return_value = mock_launcher

        service_manager.initialize_services()

        mock_bravia_cls.assert_called_once_with("192.168.1.10", "secret")
        assert service_manager.bravia_control is mock_bravia_cls.return_value

    @patch("service_manager.BraviaControl")
    @patch("service_manager.AudioMonitor")
    @patch("service_manager.MouseHandler")
    @patch("service_manager.SpeechHandler")
    @patch("service_manager.PluginRegistry")
    @patch("service_manager.RemoteSTTLauncher")
    def test_creates_audio_monitor(
        self, mock_launcher_cls, mock_plugin_cls, mock_speech_cls,
        mock_mouse_cls, mock_audio_cls, mock_bravia_cls, service_manager
    ):
        """initialize_services creates AudioMonitor."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "stt.mode": "remote",
        })
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = []
        mock_launcher_cls.return_value = mock_launcher

        service_manager.initialize_services()

        mock_audio_cls.assert_called_once()
        assert service_manager.audio_monitor is mock_audio_cls.return_value

    @patch("service_manager.BraviaControl")
    @patch("service_manager.AudioMonitor")
    @patch("service_manager.MouseHandler")
    @patch("service_manager.SpeechHandler")
    @patch("service_manager.PluginRegistry")
    @patch("service_manager.RemoteSTTLauncher")
    def test_creates_plugin_registry(
        self, mock_launcher_cls, mock_plugin_cls, mock_speech_cls,
        mock_mouse_cls, mock_audio_cls, mock_bravia_cls, service_manager
    ):
        """initialize_services creates PluginRegistry."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "stt.mode": "remote",
        })
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = []
        mock_launcher_cls.return_value = mock_launcher

        service_manager.initialize_services()

        mock_plugin_cls.assert_called_once_with(
            service_manager.config_service,
            service_manager.event_bus,
        )

    @patch("service_manager.SoftwareDimmer")
    @patch("service_manager.BraviaControl")
    @patch("service_manager.AudioMonitor")
    @patch("service_manager.MouseHandler")
    @patch("service_manager.SpeechHandler")
    @patch("service_manager.PluginRegistry")
    @patch("service_manager.RemoteSTTLauncher")
    def test_dimmer_type_overlay(
        self, mock_launcher_cls, mock_plugin_cls, mock_speech_cls,
        mock_mouse_cls, mock_audio_cls, mock_bravia_cls,
        mock_dimmer_cls, service_manager
    ):
        """initialize_services creates SoftwareDimmer for 'overlay' config."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "brightness_coordinator.software_dimmer": "software_dimmer",
            "stt.mode": "remote",
        })
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = []
        mock_launcher_cls.return_value = mock_launcher

        service_manager.initialize_services()

        mock_dimmer_cls.assert_called_once_with(service_manager.loop)
        assert service_manager.software_dimmer is mock_dimmer_cls.return_value

    @patch("service_manager.BraviaControl")
    @patch("service_manager.AudioMonitor")
    @patch("service_manager.MouseHandler")
    @patch("service_manager.SpeechHandler")
    @patch("service_manager.PluginRegistry")
    @patch("service_manager.RemoteSTTLauncher")
    def test_dimmer_type_flux_sets_none(
        self, mock_launcher_cls, mock_plugin_cls, mock_speech_cls,
        mock_mouse_cls, mock_audio_cls, mock_bravia_cls, service_manager
    ):
        """initialize_services sets software_dimmer=None for 'flux' config."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "brightness_coordinator.software_dimmer": "flux",
            "stt.mode": "remote",
        })
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = []
        mock_launcher_cls.return_value = mock_launcher

        service_manager.initialize_services()

        assert service_manager.software_dimmer is None

    @patch("service_manager.BraviaControl")
    @patch("service_manager.AudioMonitor")
    @patch("service_manager.MouseHandler")
    @patch("service_manager.SpeechHandler")
    @patch("service_manager.PluginRegistry")
    @patch("service_manager.RemoteSTTLauncher")
    def test_remote_stt_mode_creates_launcher(
        self, mock_launcher_cls, mock_plugin_cls, mock_speech_cls,
        mock_mouse_cls, mock_audio_cls, mock_bravia_cls, service_manager
    ):
        """initialize_services creates RemoteSTTLauncher in remote mode."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "stt.mode": "remote",
        })
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = [{"name": "google_stt"}]
        mock_launcher_cls.return_value = mock_launcher

        service_manager.initialize_services()

        mock_launcher_cls.assert_called_once()
        assert service_manager.remote_stt_launcher is mock_launcher
        mock_launcher.discover_providers.assert_called_once()

    @patch("service_manager.BraviaControl")
    @patch("service_manager.AudioMonitor")
    @patch("service_manager.MouseHandler")
    @patch("service_manager.SpeechHandler")
    @patch("service_manager.PluginRegistry")
    @patch("service_manager.RemoteSTTLauncher")
    def test_remote_stt_wires_launcher_to_state_manager(
        self, mock_launcher_cls, mock_plugin_cls, mock_speech_cls,
        mock_mouse_cls, mock_audio_cls, mock_bravia_cls, service_manager
    ):
        """initialize_services wires launcher to state_manager for GUI."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "stt.mode": "remote",
        })
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = []
        mock_launcher_cls.return_value = mock_launcher

        service_manager.initialize_services()

        service_manager.state_manager.set_remote_stt_launcher.assert_called_once_with(mock_launcher)


# ---------------------------------------------------------------------------
# start_services
# ---------------------------------------------------------------------------

class TestStartServices:
    """Test start_services launches background tasks."""

    def test_returns_task_list(self, service_manager):
        """start_services returns a list of tasks."""
        # Set up mock services
        mock_mouse = MagicMock()
        mock_mouse.start.return_value = [MagicMock()]
        service_manager.mouse_handler = mock_mouse

        mock_audio = MagicMock()
        mock_audio.start.return_value = MagicMock()
        service_manager.audio_monitor = mock_audio

        service_manager.software_dimmer = None
        service_manager.brightness_coordinator = None
        service_manager.plugin_registry = None

        tasks = service_manager.start_services()

        assert isinstance(tasks, list)
        assert len(tasks) >= 1

    def test_starts_software_dimmer_if_present(self, service_manager):
        """start_services calls software_dimmer.start() if set."""
        mock_dimmer = MagicMock()
        service_manager.software_dimmer = mock_dimmer
        service_manager.mouse_handler = None
        service_manager.audio_monitor = None
        service_manager.brightness_coordinator = None
        service_manager.plugin_registry = None

        service_manager.start_services()

        mock_dimmer.start.assert_called_once()

    def test_skips_software_dimmer_if_none(self, service_manager):
        """start_services doesn't crash when software_dimmer is None."""
        service_manager.software_dimmer = None
        service_manager.mouse_handler = None
        service_manager.audio_monitor = None
        service_manager.brightness_coordinator = None
        service_manager.plugin_registry = None

        # Should not raise
        tasks = service_manager.start_services()
        assert isinstance(tasks, list)

    def test_starts_brightness_coordinator(self, service_manager):
        """start_services starts brightness coordinator if present."""
        mock_coord = MagicMock()
        service_manager.brightness_coordinator = mock_coord
        service_manager.software_dimmer = None
        service_manager.mouse_handler = None
        service_manager.audio_monitor = None
        service_manager.plugin_registry = None

        service_manager.start_services()

        mock_coord.start.assert_called_once()

    def test_starts_mouse_handler(self, service_manager):
        """start_services calls mouse_handler.start() and collects tasks."""
        mock_mouse = MagicMock()
        task1, task2 = MagicMock(), MagicMock()
        mock_mouse.start.return_value = [task1, task2]
        service_manager.mouse_handler = mock_mouse
        service_manager.software_dimmer = None
        service_manager.audio_monitor = None
        service_manager.brightness_coordinator = None
        service_manager.plugin_registry = None

        tasks = service_manager.start_services()

        mock_mouse.start.assert_called_once()
        assert task1 in tasks
        assert task2 in tasks

    def test_starts_audio_monitor(self, service_manager):
        """start_services calls audio_monitor.start()."""
        mock_audio = MagicMock()
        mock_task = MagicMock()
        mock_audio.start.return_value = mock_task
        service_manager.audio_monitor = mock_audio
        service_manager.software_dimmer = None
        service_manager.mouse_handler = None
        service_manager.brightness_coordinator = None
        service_manager.plugin_registry = None

        tasks = service_manager.start_services()

        mock_audio.start.assert_called_once()
        assert mock_task in tasks


# ---------------------------------------------------------------------------
# _start_plugins
# ---------------------------------------------------------------------------

class TestStartPlugins:
    """Test _start_plugins async method."""

    @pytest.mark.asyncio
    async def test_discovers_initializes_and_starts(self, service_manager):
        """_start_plugins runs discover -> initialize -> start lifecycle."""
        mock_registry = MagicMock()
        mock_registry.discover_plugins = AsyncMock()
        mock_registry.initialize_all = AsyncMock()
        mock_registry.start_all = AsyncMock()
        service_manager.plugin_registry = mock_registry

        await service_manager._start_plugins()

        mock_registry.discover_plugins.assert_awaited_once()
        mock_registry.initialize_all.assert_awaited_once()
        mock_registry.start_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handles_plugin_error_gracefully(self, service_manager):
        """_start_plugins catches and logs errors without crashing."""
        mock_registry = MagicMock()
        mock_registry.discover_plugins = AsyncMock(side_effect=RuntimeError("plugin crash"))
        service_manager.plugin_registry = mock_registry

        # Should not raise
        await service_manager._start_plugins()

    @pytest.mark.asyncio
    async def test_noop_when_no_registry(self, service_manager):
        """_start_plugins returns early if plugin_registry is None."""
        service_manager.plugin_registry = None

        # Should not raise
        await service_manager._start_plugins()


# ---------------------------------------------------------------------------
# _build_stt_provider_kwargs
# ---------------------------------------------------------------------------

class TestBuildSttProviderKwargs:
    """Test STT provider configuration building."""

    def test_google_provider_kwargs(self, service_manager):
        """Returns language and boost_words for google provider."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "stt.google.language": "en-US",
            "stt.google.boost_words": ["hello", "world"],
        })

        result = service_manager._build_stt_provider_kwargs("google")

        assert result["language"] == "en-US"
        assert result["boost_words"] == ["hello", "world"]

    def test_google_provider_defaults(self, service_manager):
        """Returns defaults when google config not set."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({})

        result = service_manager._build_stt_provider_kwargs("google")

        assert result["language"] == "en-US"
        assert result["boost_words"] == []

    def test_azure_provider_kwargs(self, service_manager):
        """Returns subscription_key and region for azure provider."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "stt.azure.subscription_key": "my-key",
            "stt.azure.region": "westus",
        })

        result = service_manager._build_stt_provider_kwargs("azure")

        assert result["subscription_key"] == "my-key"
        assert result["region"] == "westus"

    def test_azure_provider_defaults(self, service_manager):
        """Returns defaults when azure config not set."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({})

        result = service_manager._build_stt_provider_kwargs("azure")

        assert result["subscription_key"] == ""
        assert result["region"] == "eastus"

    def test_unknown_provider_returns_empty(self, service_manager):
        """Returns empty dict for unknown provider types."""
        result = service_manager._build_stt_provider_kwargs("whisper")
        assert result == {}


# ---------------------------------------------------------------------------
# start_remote_stt
# ---------------------------------------------------------------------------

class TestStartRemoteStt:
    """Test remote STT provider startup logic."""

    def test_returns_false_without_launcher(self, service_manager):
        """Returns False when remote_stt_launcher is None."""
        service_manager.remote_stt_launcher = None
        assert service_manager.start_remote_stt() is False

    def test_returns_false_when_no_providers(self, service_manager):
        """Returns False when no providers are discovered."""
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = []
        service_manager.remote_stt_launcher = mock_launcher
        service_manager.config_service.get.side_effect = _config_get_side_effect({})

        assert service_manager.start_remote_stt() is False

    def test_starts_last_provider_from_config(self, service_manager):
        """Starts the provider specified in stt.last_provider config."""
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = [
            {"name": "google_stt"},
            {"name": "zipformer"},
        ]
        mock_launcher.start_provider.return_value = True
        service_manager.remote_stt_launcher = mock_launcher
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "stt.last_provider": "zipformer",
        })

        result = service_manager.start_remote_stt()

        assert result is True
        mock_launcher.start_provider.assert_called_once_with("zipformer")

    def test_falls_back_to_first_provider(self, service_manager):
        """Falls back to first provider when last_provider fails."""
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = [
            {"name": "google_stt"},
            {"name": "zipformer"},
        ]
        # First call (last_provider) fails, second call (first) succeeds
        mock_launcher.start_provider.side_effect = [False, True]
        service_manager.remote_stt_launcher = mock_launcher
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "stt.last_provider": "zipformer",
        })

        result = service_manager.start_remote_stt()

        assert result is True
        assert mock_launcher.start_provider.call_count == 2
        mock_launcher.start_provider.assert_called_with("google_stt")

    def test_uses_first_provider_when_no_last(self, service_manager):
        """Uses first discovered provider when no last_provider set."""
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = [
            {"name": "google_stt"},
        ]
        mock_launcher.start_provider.return_value = True
        service_manager.remote_stt_launcher = mock_launcher
        service_manager.config_service.get.side_effect = _config_get_side_effect({})

        result = service_manager.start_remote_stt()

        assert result is True
        mock_launcher.start_provider.assert_called_once_with("google_stt")

    def test_returns_false_when_all_providers_fail(self, service_manager):
        """Returns False when all provider starts fail."""
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = [
            {"name": "google_stt"},
        ]
        mock_launcher.start_provider.return_value = False
        service_manager.remote_stt_launcher = mock_launcher
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "stt.last_provider": "google_stt",
        })

        result = service_manager.start_remote_stt()

        assert result is False


# ---------------------------------------------------------------------------
# start_stt_manager
# ---------------------------------------------------------------------------

class TestStartSttManager:
    """Test in-process STT manager startup."""

    @pytest.mark.asyncio
    async def test_noop_when_no_stt_manager(self, service_manager):
        """start_stt_manager returns early when stt_manager is None."""
        service_manager.stt_manager = None

        # Should not raise
        await service_manager.start_stt_manager(Mock())

    @pytest.mark.asyncio
    async def test_registers_transcript_handler(self, service_manager):
        """start_stt_manager registers the transcript handler callback."""
        mock_stt = MagicMock()
        mock_stt.start = AsyncMock()
        service_manager.stt_manager = mock_stt
        service_manager._stt_provider_type = "google"
        service_manager._stt_provider_kwargs = {"language": "en-US"}

        handler = Mock()
        await service_manager.start_stt_manager(handler)

        mock_stt.on_transcript.assert_called_once_with(handler)

    @pytest.mark.asyncio
    async def test_starts_with_provider_config(self, service_manager):
        """start_stt_manager starts STT with provider type and kwargs."""
        mock_stt = MagicMock()
        mock_stt.start = AsyncMock()
        service_manager.stt_manager = mock_stt
        service_manager._stt_provider_type = "azure"
        service_manager._stt_provider_kwargs = {
            "subscription_key": "key",
            "region": "eastus",
        }

        await service_manager.start_stt_manager(Mock())

        mock_stt.start.assert_awaited_once_with(
            "azure",
            subscription_key="key",
            region="eastus",
        )


# ---------------------------------------------------------------------------
# Adversarial: service start() throws
# ---------------------------------------------------------------------------

class TestStartServicesExceptionHandling:
    """Test what happens when a service's start() throws during start_services."""

    def test_mouse_handler_start_exception_prevents_audio_monitor_start(
        self, service_manager
    ):
        """If mouse_handler.start() raises, audio_monitor.start() is never
        called because start_services() has no per-service error handling.

        This documents the current behavior: a crash in one service's start()
        aborts the entire start_services() call.
        """
        mock_mouse = MagicMock()
        mock_mouse.start.side_effect = RuntimeError("mouse init failed")
        service_manager.mouse_handler = mock_mouse

        mock_audio = MagicMock()
        service_manager.audio_monitor = mock_audio

        service_manager.software_dimmer = None
        service_manager.brightness_coordinator = None
        service_manager.plugin_registry = None

        with pytest.raises(RuntimeError, match="mouse init failed"):
            service_manager.start_services()

        # Audio monitor was never started because the exception aborted
        mock_audio.start.assert_not_called()

    def test_software_dimmer_start_exception_prevents_subsequent_starts(
        self, service_manager
    ):
        """If software_dimmer.start() raises, subsequent services don't start."""
        mock_dimmer = MagicMock()
        mock_dimmer.start.side_effect = OSError("display not available")
        service_manager.software_dimmer = mock_dimmer

        mock_mouse = MagicMock()
        service_manager.mouse_handler = mock_mouse

        service_manager.audio_monitor = None
        service_manager.brightness_coordinator = None
        service_manager.plugin_registry = None

        with pytest.raises(OSError, match="display not available"):
            service_manager.start_services()

        mock_mouse.start.assert_not_called()


# ---------------------------------------------------------------------------
# Adversarial: initialize_services called twice
# ---------------------------------------------------------------------------

class TestDoubleInitialization:
    """Test what happens when initialize_services is called twice."""

    @patch("service_manager.BraviaControl")
    @patch("service_manager.AudioMonitor")
    @patch("service_manager.MouseHandler")
    @patch("service_manager.SpeechHandler")
    @patch("service_manager.PluginRegistry")
    @patch("service_manager.RemoteSTTLauncher")
    def test_double_initialize_overwrites_services(
        self, mock_launcher_cls, mock_plugin_cls, mock_speech_cls,
        mock_mouse_cls, mock_audio_cls, mock_bravia_cls, service_manager
    ):
        """Calling initialize_services twice overwrites service instances.

        ServiceManager has no guard against double initialization -- each call
        creates fresh instances, replacing the old ones.
        """
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "stt.mode": "remote",
        })
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = []
        mock_launcher_cls.return_value = mock_launcher

        service_manager.initialize_services()
        first_bravia = service_manager.bravia_control
        first_audio = service_manager.audio_monitor

        # Reset mocks to create distinct new instances
        mock_bravia_cls.reset_mock()
        mock_audio_cls.reset_mock()
        new_bravia = MagicMock(name="bravia_v2")
        new_audio = MagicMock(name="audio_v2")
        mock_bravia_cls.return_value = new_bravia
        mock_audio_cls.return_value = new_audio

        service_manager.initialize_services()

        # Second init created new instances, overwriting the first
        assert service_manager.bravia_control is new_bravia
        assert service_manager.audio_monitor is new_audio
        assert service_manager.bravia_control is not first_bravia
        assert service_manager.audio_monitor is not first_audio


# ---------------------------------------------------------------------------
# Adversarial: shutdown with partially initialized services
# ---------------------------------------------------------------------------

class TestShutdownPartiallyInitialized:
    """Test shutdown_services when some services are None (partial init)."""

    @pytest.mark.asyncio
    async def test_shutdown_with_all_services_none(self, service_manager):
        """shutdown_services handles all services being None (never initialized)."""
        # Default state: all services are None
        assert service_manager.mouse_handler is None
        assert service_manager.software_dimmer is None
        assert service_manager.plugin_registry is None
        assert service_manager.stt_manager is None
        assert service_manager.brightness_coordinator is None
        assert service_manager.remote_stt_launcher is None

        # Should not raise
        await service_manager.shutdown_services()

    @pytest.mark.asyncio
    async def test_shutdown_when_plugin_stop_raises(self, service_manager):
        """shutdown_services continues shutting down other services even if
        plugin_registry.stop_all() raises, because it's wrapped in try/except.
        """
        mock_registry = MagicMock()
        mock_registry.stop_all = AsyncMock(side_effect=RuntimeError("plugin crash"))
        service_manager.plugin_registry = mock_registry

        mock_mouse = MagicMock()
        service_manager.mouse_handler = mock_mouse

        service_manager.software_dimmer = None
        service_manager.stt_manager = None
        service_manager.brightness_coordinator = None
        service_manager.remote_stt_launcher = None

        # Should not raise despite plugin crash
        await service_manager.shutdown_services()

        # Mouse handler still gets stopped
        mock_mouse.stop_listeners.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_when_stt_manager_stop_raises(self, service_manager):
        """shutdown_services continues if stt_manager.stop() raises."""
        mock_stt = MagicMock()
        mock_stt.stop = AsyncMock(side_effect=ConnectionError("socket closed"))
        service_manager.stt_manager = mock_stt

        mock_dimmer = MagicMock()
        service_manager.software_dimmer = mock_dimmer

        service_manager.mouse_handler = None
        service_manager.plugin_registry = None
        service_manager.brightness_coordinator = None
        service_manager.remote_stt_launcher = None

        # Should not raise despite STT crash
        await service_manager.shutdown_services()

        # Dimmer still gets stopped
        mock_dimmer.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_when_remote_launcher_raises(self, service_manager):
        """shutdown_services continues if remote_stt_launcher.shutdown_all_providers raises."""
        mock_launcher = MagicMock()
        mock_launcher.shutdown_all_providers = AsyncMock(
            side_effect=TimeoutError("provider hung")
        )
        service_manager.remote_stt_launcher = mock_launcher

        mock_coord = MagicMock()
        service_manager.brightness_coordinator = mock_coord

        service_manager.mouse_handler = None
        service_manager.software_dimmer = None
        service_manager.plugin_registry = None
        service_manager.stt_manager = None

        # Should not raise despite launcher crash
        await service_manager.shutdown_services()

        # Coordinator still gets stopped
        mock_coord.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_when_brightness_coordinator_stop_raises(self, service_manager):
        """shutdown_services continues if brightness_coordinator.stop() raises."""
        mock_coord = MagicMock()
        mock_coord.stop.side_effect = OSError("display handle invalid")
        service_manager.brightness_coordinator = mock_coord

        mock_mouse = MagicMock()
        service_manager.mouse_handler = mock_mouse

        service_manager.software_dimmer = None
        service_manager.plugin_registry = None
        service_manager.stt_manager = None
        service_manager.remote_stt_launcher = None

        # Should not raise - brightness coordinator stop error is caught
        await service_manager.shutdown_services()

        # Mouse handler still gets stopped
        mock_mouse.stop_listeners.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_all_services_crash_simultaneously(self, service_manager):
        """Every service crashes during shutdown - none should propagate."""
        mock_launcher = MagicMock()
        mock_launcher.shutdown_all_providers = AsyncMock(
            side_effect=RuntimeError("launcher crash")
        )
        service_manager.remote_stt_launcher = mock_launcher

        mock_registry = MagicMock()
        mock_registry.stop_all = AsyncMock(side_effect=RuntimeError("plugin crash"))
        service_manager.plugin_registry = mock_registry

        mock_stt = MagicMock()
        mock_stt.stop = AsyncMock(side_effect=RuntimeError("stt crash"))
        service_manager.stt_manager = mock_stt

        mock_coord = MagicMock()
        mock_coord.stop.side_effect = RuntimeError("coord crash")
        service_manager.brightness_coordinator = mock_coord

        # mouse_handler and software_dimmer go through asyncio.to_thread
        service_manager.mouse_handler = None
        service_manager.software_dimmer = None

        # Should not raise despite every service crashing
        await service_manager.shutdown_services()


# ---------------------------------------------------------------------------
# Adversarial: double start_services
# ---------------------------------------------------------------------------

class TestDoubleStartServices:
    """Test calling start_services twice (no guard against it)."""

    def test_double_start_calls_start_twice(self, service_manager):
        """Calling start_services twice invokes .start() on services again."""
        mock_mouse = MagicMock()
        mock_mouse.start.return_value = [MagicMock()]
        service_manager.mouse_handler = mock_mouse

        mock_audio = MagicMock()
        mock_audio.start.return_value = MagicMock()
        service_manager.audio_monitor = mock_audio

        service_manager.software_dimmer = None
        service_manager.brightness_coordinator = None
        service_manager.plugin_registry = None

        service_manager.start_services()
        service_manager.start_services()

        # Both services had start() called twice
        assert mock_mouse.start.call_count == 2
        assert mock_audio.start.call_count == 2


# ---------------------------------------------------------------------------
# AIService integration
# ---------------------------------------------------------------------------

class TestAIServiceIntegration:
    """Test AIService wiring in ServiceManager."""

    @patch("service_manager.BraviaControl")
    @patch("service_manager.AudioMonitor")
    @patch("service_manager.MouseHandler")
    @patch("service_manager.SpeechHandler")
    @patch("service_manager.PluginRegistry")
    @patch("service_manager.RemoteSTTLauncher")
    @patch("service_manager.AIService")
    def test_ai_service_created_during_initialize(
        self, mock_ai_cls, mock_launcher_cls, mock_plugin_cls,
        mock_speech_cls, mock_mouse_cls, mock_audio_cls,
        mock_bravia_cls, service_manager
    ):
        """initialize_services creates AIService when ai.enabled is true."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "stt.mode": "remote",
            "ai.enabled": True,
        })
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = []
        mock_launcher_cls.return_value = mock_launcher

        service_manager.initialize_services()

        mock_ai_cls.assert_called_once_with(
            config_service=service_manager.config_service,
        )
        assert service_manager.ai_service is mock_ai_cls.return_value

    @patch("service_manager.BraviaControl")
    @patch("service_manager.AudioMonitor")
    @patch("service_manager.MouseHandler")
    @patch("service_manager.SpeechHandler")
    @patch("service_manager.PluginRegistry")
    @patch("service_manager.RemoteSTTLauncher")
    @patch("service_manager.AIService")
    def test_ai_service_not_created_when_disabled(
        self, mock_ai_cls, mock_launcher_cls, mock_plugin_cls,
        mock_speech_cls, mock_mouse_cls, mock_audio_cls,
        mock_bravia_cls, service_manager
    ):
        """initialize_services skips AIService when ai.enabled is false."""
        service_manager.config_service.get.side_effect = _config_get_side_effect({
            "stt.mode": "remote",
            "ai.enabled": False,
        })
        mock_launcher = MagicMock()
        mock_launcher.discover_providers.return_value = []
        mock_launcher_cls.return_value = mock_launcher

        service_manager.initialize_services()

        mock_ai_cls.assert_not_called()
        assert service_manager.ai_service is None

    @pytest.mark.asyncio
    async def test_start_ai_service_calls_start(self, service_manager):
        """_start_ai_service calls ai_service.start()."""
        mock_ai = MagicMock()
        mock_ai.start = AsyncMock()
        service_manager.ai_service = mock_ai

        await service_manager._start_ai_service()

        mock_ai.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_ai_service_error_does_not_propagate(self, service_manager):
        """_start_ai_service catches errors from ai_service.start()."""
        mock_ai = MagicMock()
        mock_ai.start = AsyncMock(side_effect=RuntimeError("ollama not running"))
        service_manager.ai_service = mock_ai

        # Should not raise
        await service_manager._start_ai_service()

    def test_start_services_creates_ai_task(self, service_manager):
        """start_services creates a task for AI service startup."""
        mock_ai = MagicMock()
        service_manager.ai_service = mock_ai

        service_manager.software_dimmer = None
        service_manager.brightness_coordinator = None
        service_manager.mouse_handler = None
        service_manager.audio_monitor = None
        service_manager.plugin_registry = None

        tasks = service_manager.start_services()

        # Should have created a task for _start_ai_service
        assert len(tasks) == 1

    @pytest.mark.asyncio
    async def test_ai_service_started_in_shutdown(self, service_manager):
        """shutdown_services calls ai_service.stop() if present."""
        mock_ai = MagicMock()
        mock_ai.stop = AsyncMock()
        service_manager.ai_service = mock_ai

        service_manager.mouse_handler = None
        service_manager.software_dimmer = None
        service_manager.plugin_registry = None
        service_manager.stt_manager = None
        service_manager.brightness_coordinator = None
        service_manager.remote_stt_launcher = None

        await service_manager.shutdown_services()

        mock_ai.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_ai_service_error_does_not_propagate(self, service_manager):
        """shutdown_services catches errors from ai_service.stop()."""
        mock_ai = MagicMock()
        mock_ai.stop = AsyncMock(side_effect=RuntimeError("ai crash"))
        service_manager.ai_service = mock_ai

        service_manager.mouse_handler = None
        service_manager.software_dimmer = None
        service_manager.plugin_registry = None
        service_manager.stt_manager = None
        service_manager.brightness_coordinator = None
        service_manager.remote_stt_launcher = None

        # Should not raise
        await service_manager.shutdown_services()

    @pytest.mark.asyncio
    async def test_shutdown_with_no_ai_service(self, service_manager):
        """shutdown_services handles ai_service being None."""
        service_manager.ai_service = None

        service_manager.mouse_handler = None
        service_manager.software_dimmer = None
        service_manager.plugin_registry = None
        service_manager.stt_manager = None
        service_manager.brightness_coordinator = None
        service_manager.remote_stt_launcher = None

        # Should not raise
        await service_manager.shutdown_services()
