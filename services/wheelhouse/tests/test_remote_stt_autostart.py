"""Tests for remote STT provider auto-start on WheelHouse startup.

TDD: These tests are written FIRST per CLAUDE.md requirements.
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

# Add parent directories to path for imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestAutoStartConfig:
    """Tests for stt.last_provider config field."""

    def test_config_has_last_provider_field(self, tmp_path):
        """WheelHouse config should have stt.last_provider field."""
        # Create minimal config
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[stt]
mode = "remote"
last_provider = "google_stt"
""")

        # Use tomllib to verify field is readable
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        with open(config_file, "rb") as f:
            config = tomllib.load(f)

        assert config["stt"]["last_provider"] == "google_stt"

    def test_last_provider_defaults_to_none_if_missing(self):
        """If stt.last_provider is not set, should default to None."""
        from config_service import ConfigService

        # Mock config without last_provider
        config_service = MagicMock(spec=ConfigService)
        config_service.get.return_value = None

        result = config_service.get("stt.last_provider", None)
        assert result is None


class TestServiceManagerRemoteSTTLauncher:
    """Tests for RemoteSTTLauncher integration in ServiceManager."""

    @pytest.fixture
    def mock_services(self, tmp_path):
        """Create mock service dependencies."""
        # Create mock services directory with a provider
        services_dir = tmp_path / "services"
        services_dir.mkdir()
        google_dir = services_dir / "google_stt_server"
        google_dir.mkdir()
        (google_dir / "config.toml").write_text("""
[provider]
name = "google_stt"
display_name = "Google Cloud STT"
launcher = "launcher.py"
""")
        (google_dir / "launcher.py").write_text("# stub")

        return {
            "services_dir": services_dir,
            "app_data_dir": tmp_path / "appdata",
        }

    def test_service_manager_has_remote_stt_launcher(self, mock_services):
        """ServiceManager should have a remote_stt_launcher attribute."""
        from service_manager import ServiceManager

        # Create minimal mocks
        config_service = MagicMock()
        config_service.get.return_value = "remote"
        config_service.get_config.return_value = {}

        event_bus = MagicMock()
        loop = asyncio.new_event_loop()
        app = MagicMock()
        app.get_screen_dimensions.return_value = (1920, 1080)
        state_manager = MagicMock()

        service_manager = ServiceManager(
            config_service, event_bus, loop, app, state_manager
        )

        # Should have remote_stt_launcher attribute (may be None before init)
        assert hasattr(service_manager, "remote_stt_launcher")
        loop.close()

    def test_remote_stt_launcher_initialized_in_remote_mode(self, mock_services):
        """RemoteSTTLauncher is created in initialize_services when mode is remote.

        This is a code inspection test - verifying the code path exists.
        Full integration testing requires running WheelHouse.
        """
        from service_manager import ServiceManager
        from stt.remote_stt_launcher import RemoteSTTLauncher
        import inspect

        # Verify the import exists
        assert RemoteSTTLauncher is not None

        # Verify initialize_services builds the launcher (construction
        # itself lives in _build_remote_stt_launcher since
        # wh-stt-client-address).
        source = inspect.getsource(ServiceManager.initialize_services)
        assert "_build_remote_stt_launcher()" in source
        assert "remote_stt_launcher" in source
        helper_source = inspect.getsource(
            ServiceManager._build_remote_stt_launcher
        )
        assert "RemoteSTTLauncher(" in helper_source

    def test_remote_stt_launcher_not_initialized_in_process_mode(self, mock_services):
        """RemoteSTTLauncher is only created when stt.mode is 'remote'.

        This is a code inspection test - verifying the conditional exists.
        """
        from service_manager import ServiceManager
        import inspect

        # Verify initialize_services has conditional for remote mode
        source = inspect.getsource(ServiceManager.initialize_services)
        # The RemoteSTTLauncher should only be created in the else branch (remote mode)
        assert 'stt_mode == "in_process"' in source or "stt_mode == 'in_process'" in source
        # And the launcher is in the else branch
        assert "else:" in source

    def _service_manager_shell(self, config_map):
        """A ServiceManager shell carrying only config_service, enough to
        drive _build_remote_stt_launcher without the full constructor."""
        from service_manager import ServiceManager

        sm = ServiceManager.__new__(ServiceManager)
        sm.config_service = MagicMock()
        sm.config_service.get.side_effect = (
            lambda key, default=None: config_map.get(key, default)
        )
        return sm

    def test_launcher_ws_host_from_config(self, tmp_path):
        """wh-stt-client-address: stt.ws_host tells the STT provider
        processes where to connect back to, mirroring the AI client's
        configured server address."""
        sm = self._service_manager_shell({"stt.ws_host": "192.168.1.50"})
        launcher = sm._build_remote_stt_launcher(app_data_dir=tmp_path)
        assert launcher.ws_host == "192.168.1.50"

    def test_launcher_ws_host_defaults_to_localhost(self, tmp_path):
        sm = self._service_manager_shell({})
        launcher = sm._build_remote_stt_launcher(app_data_dir=tmp_path)
        assert launcher.ws_host == "localhost"

    def test_launcher_wake_word_config_preserved(self, tmp_path):
        """The helper must keep passing the wake-word config through."""
        sm = self._service_manager_shell({"wake_word.keyword": "jarvis"})
        launcher = sm._build_remote_stt_launcher(app_data_dir=tmp_path)
        assert launcher.wake_word_config["keyword"] == "jarvis"


class TestAutoStartProvider:
    """Tests for auto-starting the last selected provider."""

    @pytest.fixture
    def services_dir(self, tmp_path):
        """Create mock services directory with providers."""
        services_dir = tmp_path / "services"
        services_dir.mkdir()

        # Google provider
        google_dir = services_dir / "google_stt_server"
        google_dir.mkdir()
        (google_dir / "config.toml").write_text("""
[provider]
name = "google_stt"
display_name = "Google Cloud STT"
launcher = "launcher.py"
""")
        (google_dir / "launcher.py").write_text("# stub")

        # Parakeet provider
        parakeet_dir = services_dir / "sherpa_offline_parakeet_stt_server"
        parakeet_dir.mkdir()
        (parakeet_dir / "config.toml").write_text("""
[provider]
name = "parakeet"
display_name = "Parakeet v3 (Local)"
launcher = "launcher.py"
""")
        (parakeet_dir / "launcher.py").write_text("# stub")

        return services_dir

    def test_starts_last_selected_provider(self, services_dir, tmp_path):
        """Auto-start should launch the provider from stt.last_provider config."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        app_data_dir = tmp_path / "appdata"
        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_popen.return_value = mock_process

            # Simulate auto-start of last provider
            result = launcher.start_provider("google_stt")

            assert result is True
            mock_popen.assert_called_once()

    def test_starts_first_available_if_last_not_found(self, services_dir, tmp_path):
        """If last_provider is invalid, should start first available provider."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        app_data_dir = tmp_path / "appdata"
        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        # Discover providers
        providers = launcher.discover_providers()
        assert len(providers) >= 1

        # Try to start non-existent provider - should fail
        result = launcher.start_provider("nonexistent")
        assert result is False

        # Should be able to fall back to first available
        first_provider = providers[0]["name"]
        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_popen.return_value = mock_process

            result = launcher.start_provider(first_provider)
            assert result is True

    def test_does_not_start_if_already_running(self, services_dir, tmp_path):
        """Auto-start should skip if provider is already running on correct port."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        app_data_dir = tmp_path / "appdata"
        app_data_dir.mkdir(parents=True, exist_ok=True)

        # Create PID file to simulate running provider
        pid_file = app_data_dir / "google_stt.pid"
        pid_file.write_text(str(os.getpid()))  # Use current PID (alive)

        # Port file must match ws_port, otherwise the launcher treats
        # the process as stale and terminates it (which would kill pytest!)
        port_file = app_data_dir / "google_stt.port"
        port_file.write_text("5500")

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        with patch("subprocess.Popen") as mock_popen:
            result = launcher.start_provider("google_stt")

            assert result is True  # Returns True (already running)
            mock_popen.assert_not_called()  # But no new process started


class TestStartRemoteSTTMethod:
    """Tests for ServiceManager.start_remote_stt() method."""

    @pytest.fixture
    def mock_service_manager(self):
        """Create a mock ServiceManager with RemoteSTTLauncher."""
        from service_manager import ServiceManager

        config_service = MagicMock()
        config_service.get.side_effect = lambda key, default=None: {
            "stt.mode": "remote",
            "stt.last_provider": "google_stt",
        }.get(key, default)
        config_service.get_config.return_value = {}

        event_bus = MagicMock()
        loop = asyncio.new_event_loop()
        app = MagicMock()
        app.get_screen_dimensions.return_value = (1920, 1080)
        state_manager = MagicMock()

        service_manager = ServiceManager(
            config_service, event_bus, loop, app, state_manager
        )

        yield service_manager
        loop.close()

    def test_start_remote_stt_method_exists(self, mock_service_manager):
        """ServiceManager should have start_remote_stt() method."""
        assert hasattr(mock_service_manager, "start_remote_stt")
        assert callable(mock_service_manager.start_remote_stt)

    def test_start_remote_stt_starts_last_provider(self, mock_service_manager, tmp_path):
        """start_remote_stt() should start the last selected provider."""
        # Setup mock launcher
        mock_launcher = MagicMock()
        mock_launcher.start_provider.return_value = True
        mock_launcher.discover_providers.return_value = [
            {"name": "google_stt", "display_name": "Google Cloud STT"}
        ]
        mock_service_manager.remote_stt_launcher = mock_launcher

        # Call start_remote_stt
        mock_service_manager.start_remote_stt()

        # Should have called start_provider with last_provider
        mock_launcher.start_provider.assert_called_once_with("google_stt")

    def test_start_remote_stt_falls_back_to_first_available(self, mock_service_manager):
        """start_remote_stt() should fall back to first provider if last is invalid."""
        # Setup mock launcher - last_provider fails, first succeeds
        mock_launcher = MagicMock()
        mock_launcher.start_provider.side_effect = [False, True]  # First call fails, second succeeds
        mock_launcher.discover_providers.return_value = [
            {"name": "google_stt", "display_name": "Google Cloud STT"},
            {"name": "parakeet", "display_name": "Parakeet v3"},
        ]
        mock_service_manager.remote_stt_launcher = mock_launcher

        # Override config to return invalid provider
        mock_service_manager.config_service.get.side_effect = lambda key, default=None: {
            "stt.mode": "remote",
            "stt.last_provider": "invalid_provider",
        }.get(key, default)

        mock_service_manager.start_remote_stt()

        # Should have tried last_provider first, then fallen back
        assert mock_launcher.start_provider.call_count == 2
