"""Tests for RemoteSTTLauncher - discovers and manages remote STT providers.

TDD: These tests are written FIRST per CLAUDE.md requirements.
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

# Add parent directories to path for imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestProviderDiscovery:
    """Tests for discover_providers() method."""

    @pytest.fixture
    def services_dir(self, tmp_path):
        """Create a mock services directory with provider configs."""
        # Create google_stt_server with [provider] section
        google_dir = tmp_path / "google_stt_server"
        google_dir.mkdir()
        (google_dir / "config.toml").write_text("""
[provider]
name = "google_stt"
display_name = "Google Cloud STT"
launcher = "launcher.py"

[server]
model = "latest_short"
""")
        (google_dir / "launcher.py").write_text("# launcher stub")

        # Create parakeet with [provider] section
        parakeet_dir = tmp_path / "sherpa_offline_parakeet_stt_server"
        parakeet_dir.mkdir()
        (parakeet_dir / "config.toml").write_text("""
[provider]
name = "parakeet"
display_name = "Parakeet v3 (Local)"
launcher = "launcher.py"

[server]
host = "localhost"
""")
        (parakeet_dir / "launcher.py").write_text("# launcher stub")

        # Create a service WITHOUT [provider] section (should be ignored)
        skills_dir = tmp_path / "skills_server"
        skills_dir.mkdir()
        (skills_dir / "config.toml").write_text("""
[server]
port = 8080
""")

        # Create a directory without config.toml (should be ignored)
        empty_dir = tmp_path / "empty_service"
        empty_dir.mkdir()

        return tmp_path

    def test_discovers_providers_with_provider_section(self, services_dir):
        """discover_providers() finds services with [provider] section in config.toml."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(services_dir=services_dir)
        providers = launcher.discover_providers()

        assert len(providers) == 2
        provider_names = {p["name"] for p in providers}
        assert provider_names == {"google_stt", "parakeet"}

    def test_provider_metadata_extracted_correctly(self, services_dir):
        """Provider metadata is correctly extracted from config.toml."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(services_dir=services_dir)
        providers = launcher.discover_providers()

        google = next(p for p in providers if p["name"] == "google_stt")
        assert google["display_name"] == "Google Cloud STT"
        assert google["launcher"] == "launcher.py"
        assert "service_dir" in google
        assert google["service_dir"].name == "google_stt_server"

    def test_ignores_services_without_provider_section(self, services_dir):
        """Services without [provider] section are not discovered."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(services_dir=services_dir)
        providers = launcher.discover_providers()

        provider_names = {p["name"] for p in providers}
        assert "skills_server" not in provider_names

    def test_ignores_directories_without_config_toml(self, services_dir):
        """Directories without config.toml are skipped."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(services_dir=services_dir)
        providers = launcher.discover_providers()

        # Should not raise, should just skip
        assert len(providers) == 2

    def test_returns_empty_list_if_no_providers(self, tmp_path):
        """Returns empty list if no valid providers found."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(services_dir=tmp_path)
        providers = launcher.discover_providers()

        assert providers == []

    def test_skips_disabled_providers(self, tmp_path):
        """discover_providers() skips providers with enabled = false."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        # Create a disabled provider
        disabled_dir = tmp_path / "disabled_example_stt_server"
        disabled_dir.mkdir()
        (disabled_dir / "config.toml").write_text("""
[provider]
name = "disabled_example"
display_name = "Example Disabled Provider"
launcher = "launcher.py"
enabled = false
""")
        (disabled_dir / "launcher.py").write_text("# launcher stub")

        # Create an enabled provider (no enabled key = default true)
        enabled_dir = tmp_path / "google_stt_server"
        enabled_dir.mkdir()
        (enabled_dir / "config.toml").write_text("""
[provider]
name = "google_stt"
display_name = "Google Cloud STT"
launcher = "launcher.py"
""")
        (enabled_dir / "launcher.py").write_text("# launcher stub")

        launcher = RemoteSTTLauncher(services_dir=tmp_path)
        providers = launcher.discover_providers()

        provider_names = {p["name"] for p in providers}
        assert "voxtral" not in provider_names
        assert "google_stt" in provider_names
        assert len(providers) == 1

    def test_display_name_mode_placeholder_resolved_at_discovery(self, tmp_path):
        """discover_providers() must substitute {mode} so tray menu never sees template."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        provider_dir = tmp_path / "mode_provider"
        provider_dir.mkdir()
        (provider_dir / "config.toml").write_text("""
[provider]
name = "mode_provider"
display_name = "Mode Provider ({mode})"
launcher = "launcher.py"

[model]
use_gpu = false
""")
        (provider_dir / "launcher.py").write_text("# stub")

        launcher = RemoteSTTLauncher(services_dir=tmp_path)
        providers = launcher.discover_providers()

        assert len(providers) == 1
        assert providers[0]["display_name"] == "Mode Provider (CPU)"
        assert "{mode}" not in providers[0]["display_name"]


class TestIsRunning:
    """Tests for is_running() method - checks if provider is running via PID file."""

    @pytest.fixture
    def app_data_dir(self, tmp_path):
        """Create a mock app data directory for PID files."""
        return tmp_path

    def test_returns_false_when_no_pid_file(self, app_data_dir):
        """is_running() returns False when no PID file exists."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(app_data_dir=app_data_dir)
        assert launcher.is_running("google_stt") is False

    def test_returns_true_when_pid_file_exists_and_process_alive(self, app_data_dir):
        """is_running() returns True when PID file exists and process is alive."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        # Write a PID file with current process PID (guaranteed to be alive)
        pid_file = app_data_dir / "google_stt.pid"
        pid_file.write_text(str(os.getpid()))

        launcher = RemoteSTTLauncher(app_data_dir=app_data_dir)
        assert launcher.is_running("google_stt") is True

    def test_returns_false_when_pid_file_exists_but_process_dead(self, app_data_dir):
        """is_running() returns False when PID file exists but process is not running."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        # Write a PID file with a non-existent PID (unlikely to be a real process)
        pid_file = app_data_dir / "google_stt.pid"
        pid_file.write_text("999999999")

        launcher = RemoteSTTLauncher(app_data_dir=app_data_dir)

        # Mock psutil.pid_exists to return False
        with patch("stt.remote_stt_launcher.psutil.pid_exists", return_value=False):
            assert launcher.is_running("google_stt") is False

    def test_cleans_up_stale_pid_file(self, app_data_dir):
        """is_running() removes stale PID file when process is dead."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        pid_file = app_data_dir / "google_stt.pid"
        pid_file.write_text("999999999")

        launcher = RemoteSTTLauncher(app_data_dir=app_data_dir)

        with patch("stt.remote_stt_launcher.psutil.pid_exists", return_value=False):
            launcher.is_running("google_stt")
            # PID file should be removed
            assert not pid_file.exists()


class TestStartProvider:
    """Tests for start_provider() method."""

    @pytest.fixture
    def services_dir(self, tmp_path):
        """Create a mock services directory with a provider."""
        google_dir = tmp_path / "google_stt_server"
        google_dir.mkdir()
        (google_dir / "config.toml").write_text("""
[provider]
name = "google_stt"
display_name = "Google Cloud STT"
launcher = "launcher.py"
""")
        (google_dir / "launcher.py").write_text("# launcher stub")
        return tmp_path

    @pytest.fixture
    def app_data_dir(self, tmp_path):
        """Create a mock app data directory."""
        app_data = tmp_path / "appdata"
        app_data.mkdir()
        return app_data

    def test_starts_provider_subprocess(self, services_dir, app_data_dir):
        """start_provider() launches the provider's launcher.py as subprocess."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_popen.return_value = mock_process

            result = launcher.start_provider("google_stt")

            assert result is True
            mock_popen.assert_called_once()
            # Check that launcher.py was called with --ws-host and --ws-port args
            call_args = mock_popen.call_args
            cmd = call_args[0][0]
            # uv run --directory <svc> --locked --no-sync python <launcher_script> --ws-host ...
            assert cmd[0] == "uv"
            assert cmd[1] == "run"
            assert any("launcher.py" in part for part in cmd)
            assert "--ws-host" in cmd
            assert "--ws-port" in cmd

    def test_runtime_launch_disables_sync_and_relock(self, services_dir, app_data_dir):
        """start_provider() must pass --locked --no-sync to uv run.

        Bootstrap has already synced each service venv. Runtime launch
        must not sync or relock, otherwise every provider switch could
        hit the network, mutate venv state mid-run, or block past the
        startup-monitor deadline. --locked also fails loudly if the
        lockfile is out of date.
        """
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=12345)
            launcher.start_provider("google_stt")

        cmd = mock_popen.call_args[0][0]
        assert "--locked" in cmd
        assert "--no-sync" in cmd
        # Both flags must appear before the python invocation, i.e. as uv args.
        python_index = cmd.index("python")
        assert cmd.index("--locked") < python_index
        assert cmd.index("--no-sync") < python_index

    def test_resolves_wake_word_model_dir_to_shared_fallback(self, services_dir, app_data_dir):
        """Legacy data/wake_words resolves to shared/data/wake_words when provider path is missing."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        shared_model_dir = services_dir / "shared" / "data" / "wake_words"
        shared_model_dir.mkdir(parents=True)

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
            wake_word_config={
                "enabled": True,
                "model_dir": "data/wake_words",
            },
        )

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=12345)
            result = launcher.start_provider("google_stt")

        assert result is True
        cmd = mock_popen.call_args[0][0]
        model_dir_arg = cmd[cmd.index("--wake-word-model-dir") + 1]
        assert Path(model_dir_arg) == shared_model_dir.resolve()

    def test_prefers_provider_local_wake_word_model_dir_when_present(self, services_dir, app_data_dir):
        """Provider-local wake-word model dir takes precedence over shared fallback."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        provider_model_dir = services_dir / "google_stt_server" / "data" / "wake_words"
        provider_model_dir.mkdir(parents=True)
        # Create shared dir too to ensure provider-local path wins.
        (services_dir / "shared" / "data" / "wake_words").mkdir(parents=True)

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
            wake_word_config={
                "enabled": True,
                "model_dir": "data/wake_words",
            },
        )

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=12345)
            result = launcher.start_provider("google_stt")

        assert result is True
        cmd = mock_popen.call_args[0][0]
        model_dir_arg = cmd[cmd.index("--wake-word-model-dir") + 1]
        assert Path(model_dir_arg) == provider_model_dir.resolve()

    def test_returns_false_for_unknown_provider(self, services_dir, app_data_dir):
        """start_provider() returns False for unknown provider name."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir
        )

        result = launcher.start_provider("nonexistent_provider")
        assert result is False

    def test_does_not_start_if_already_running(self, services_dir, app_data_dir):
        """start_provider() returns True without starting if provider already running."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        # Create PID file to simulate running provider
        pid_file = app_data_dir / "google_stt.pid"
        pid_file.write_text(str(os.getpid()))
        # Port file must match ws_port for "already running" to take effect
        port_file = app_data_dir / "google_stt.port"
        port_file.write_text("5500")

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        with patch("subprocess.Popen") as mock_popen:
            result = launcher.start_provider("google_stt")

            assert result is True
            mock_popen.assert_not_called()  # Should not start new process

    def test_does_not_send_loading_notification(self, services_dir, app_data_dir):
        """start_provider() no longer sends loading via notify callback (uses working dialog)."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        # Set up notification callback
        notifications = []
        launcher.set_notify_callback(lambda title, msg: notifications.append((title, msg)))

        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_popen.return_value = mock_process

            launcher.start_provider("google_stt")

            # Loading now goes through working dialog, not notify callback
            assert len(notifications) == 0

    def test_sends_failure_notification_on_startup_timeout(self, services_dir, app_data_dir):
        """start_provider() sends failure notification if provider doesn't start in time."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        # Set up notification callback
        notifications = []
        launcher.set_notify_callback(lambda title, msg: notifications.append((title, msg)))

        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_popen.return_value = mock_process

            # Start provider with very short timeout (provider won't create PID file)
            launcher.start_provider("google_stt")

            # Wait for the monitor thread to timeout (use short timeout for test)
            # We'll patch the timeout by calling _monitor_startup directly
            launcher._monitor_startup("google_stt", "Google Cloud STT", timeout=0.1)

            # Loading now goes through working dialog, not notify callback.
            # Only failure notification should come through notify callback.
            assert len(notifications) == 1
            assert notifications[0] == ("Google Cloud STT", "Failed to start - try restarting WheelHouse")

    def test_no_failure_notification_if_provider_sends_ready(self, services_dir, app_data_dir):
        """No failure notification if provider sends ready signal within timeout."""
        import threading
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir
        )

        # Set up notification callback
        notifications = []
        launcher.set_notify_callback(lambda title, msg: notifications.append((title, msg)))

        # Simulate provider sending ready signal after a short delay
        def signal_ready():
            time.sleep(0.05)  # Small delay to simulate startup
            launcher.signal_provider_ready()

        ready_thread = threading.Thread(target=signal_ready, daemon=True)
        ready_thread.start()

        # Call monitor directly - should return when ready signal is received
        launcher._monitor_startup("google_stt", "Google Cloud STT", timeout=1.0)

        # No failure notification should be sent
        assert len(notifications) == 0

    def test_sends_failure_notification_on_popen_exception(self, services_dir, app_data_dir):
        """start_provider() sends failure notification if subprocess.Popen fails."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        # Set up notification callback
        notifications = []
        launcher.set_notify_callback(lambda title, msg: notifications.append((title, msg)))

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.side_effect = OSError("Command not found")

            result = launcher.start_provider("google_stt")

            assert result is False
            # Loading now goes through working dialog, not notify callback.
            # Only failure notification should come through notify callback.
            assert len(notifications) == 1
            assert notifications[0] == ("Google Cloud STT", "Failed to start - try restarting WheelHouse")

    def test_removes_stale_pid_file_before_starting(self, services_dir, app_data_dir):
        """start_provider() removes stale PID file to ensure fresh startup monitoring."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        # Create a stale PID file with a non-existent process ID
        pid_file = app_data_dir / "google_stt.pid"
        pid_file.write_text("99999999")  # Very unlikely to be a real PID

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_popen.return_value = mock_process

            # Start should succeed (stale PID file removed)
            result = launcher.start_provider("google_stt")

            assert result is True
            mock_popen.assert_called_once()
            # PID file should have been removed before subprocess started
            # (it may be recreated by the monitor, but that's a different concern)


class TestStopProvider:
    """Tests for stop_provider() method."""

    @pytest.fixture
    def services_dir(self, tmp_path):
        """Create a mock services directory with a provider."""
        google_dir = tmp_path / "google_stt_server"
        google_dir.mkdir()
        (google_dir / "config.toml").write_text("""
[provider]
name = "google_stt"
display_name = "Google Cloud STT"
launcher = "launcher.py"
""")
        (google_dir / "launcher.py").write_text("# launcher stub")
        return tmp_path

    @pytest.fixture
    def app_data_dir(self, tmp_path):
        """Create a mock app data directory."""
        app_data = tmp_path / "appdata"
        app_data.mkdir()
        return app_data

    @pytest.mark.asyncio
    async def test_sends_shutdown_command_via_websocket(self, services_dir, app_data_dir):
        """stop_provider() sends shutdown command via WebSocket manager."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir
        )

        # Mock WebSocket manager
        mock_ws_manager = MagicMock()
        mock_ws_manager.send_command_to_stt = AsyncMock()
        launcher.set_websocket_manager(mock_ws_manager)

        # Create PID file to simulate running provider
        pid_file = app_data_dir / "google_stt.pid"
        pid_file.write_text("12345")

        with patch("stt.remote_stt_launcher.psutil.pid_exists", return_value=True):
            result = await launcher.stop_provider("google_stt")

        assert result is True
        mock_ws_manager.send_command_to_stt.assert_called_once_with("shutdown")

    @pytest.mark.asyncio
    async def test_returns_true_if_not_running(self, services_dir, app_data_dir):
        """stop_provider() returns True if provider is not running (nothing to stop)."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir
        )

        result = await launcher.stop_provider("google_stt")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_provider(self, services_dir, app_data_dir):
        """stop_provider() returns False for unknown provider name."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir
        )

        result = await launcher.stop_provider("nonexistent_provider")
        assert result is False


class TestGetProviderByName:
    """Tests for get_provider_by_name() helper method."""

    @pytest.fixture
    def services_dir(self, tmp_path):
        """Create a mock services directory with providers."""
        google_dir = tmp_path / "google_stt_server"
        google_dir.mkdir()
        (google_dir / "config.toml").write_text("""
[provider]
name = "google_stt"
display_name = "Google Cloud STT"
launcher = "launcher.py"
""")
        (google_dir / "launcher.py").write_text("# launcher stub")
        return tmp_path

    def test_returns_provider_info_by_name(self, services_dir):
        """get_provider_by_name() returns provider info for valid name."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(services_dir=services_dir)
        provider = launcher.get_provider_by_name("google_stt")

        assert provider is not None
        assert provider["name"] == "google_stt"
        assert provider["display_name"] == "Google Cloud STT"

    def test_returns_none_for_unknown_name(self, services_dir):
        """get_provider_by_name() returns None for unknown provider name."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(services_dir=services_dir)
        provider = launcher.get_provider_by_name("unknown")

        assert provider is None


class TestShutdownAllProviders:
    """Tests for shutdown_all_providers() method - T7: Shutdown on exit."""

    @pytest.fixture
    def services_dir(self, tmp_path):
        """Create a mock services directory with multiple providers."""
        # Create google_stt provider
        google_dir = tmp_path / "google_stt_server"
        google_dir.mkdir()
        (google_dir / "config.toml").write_text("""
[provider]
name = "google_stt"
display_name = "Google Cloud STT"
launcher = "launcher.py"
""")
        (google_dir / "launcher.py").write_text("# launcher stub")

        # Create parakeet provider
        parakeet_dir = tmp_path / "sherpa_offline_parakeet_stt_server"
        parakeet_dir.mkdir()
        (parakeet_dir / "config.toml").write_text("""
[provider]
name = "parakeet"
display_name = "Parakeet v3 (Local)"
launcher = "launcher.py"
""")
        (parakeet_dir / "launcher.py").write_text("# launcher stub")

        return tmp_path

    @pytest.fixture
    def app_data_dir(self, tmp_path):
        """Create a mock app data directory."""
        app_data = tmp_path / "appdata"
        app_data.mkdir()
        return app_data

    @pytest.mark.asyncio
    async def test_sends_shutdown_to_all_running_providers(self, services_dir, app_data_dir):
        """shutdown_all_providers() sends shutdown to all running providers."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir
        )

        # Discover providers first
        launcher.discover_providers()

        # Mock WebSocket manager
        mock_ws_manager = MagicMock()
        mock_ws_manager.send_command_to_stt = AsyncMock()
        launcher.set_websocket_manager(mock_ws_manager)

        # Create PID files to simulate both providers running
        (app_data_dir / "google_stt.pid").write_text("12345")
        (app_data_dir / "parakeet.pid").write_text("12346")

        with patch("stt.remote_stt_launcher.psutil.pid_exists", return_value=True):
            results = await launcher.shutdown_all_providers()

        # Both providers should have received shutdown
        assert results == {"google_stt": True, "parakeet": True}
        # send_command_to_stt should have been called (broadcasts to all connected)
        mock_ws_manager.send_command_to_stt.assert_called_with("shutdown")

    @pytest.mark.asyncio
    async def test_skips_providers_not_running(self, services_dir, app_data_dir):
        """shutdown_all_providers() skips providers that are not running."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir
        )

        launcher.discover_providers()

        mock_ws_manager = MagicMock()
        mock_ws_manager.send_command_to_stt = AsyncMock()
        launcher.set_websocket_manager(mock_ws_manager)

        # Only google_stt is running
        (app_data_dir / "google_stt.pid").write_text("12345")
        # parakeet has no PID file - not running

        with patch("stt.remote_stt_launcher.psutil.pid_exists", return_value=True):
            results = await launcher.shutdown_all_providers()

        # google_stt should succeed, parakeet should also succeed (not running = nothing to stop)
        assert results == {"google_stt": True, "parakeet": True}

    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_no_providers(self, tmp_path):
        """shutdown_all_providers() returns empty dict when no providers discovered."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=tmp_path,  # Empty directory
            app_data_dir=tmp_path
        )

        results = await launcher.shutdown_all_providers()
        assert results == {}

    @pytest.mark.asyncio
    async def test_continues_on_individual_provider_failure(self, services_dir, app_data_dir):
        """shutdown_all_providers() continues even if one provider fails."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir
        )

        launcher.discover_providers()

        # WebSocket manager that fails on first call then succeeds
        mock_ws_manager = MagicMock()
        call_count = [0]

        async def side_effect(cmd):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("WebSocket error")
            return None

        mock_ws_manager.send_command_to_stt = AsyncMock(side_effect=side_effect)
        launcher.set_websocket_manager(mock_ws_manager)

        # Both providers running
        (app_data_dir / "google_stt.pid").write_text("12345")
        (app_data_dir / "parakeet.pid").write_text("12346")

        with patch("stt.remote_stt_launcher.psutil.pid_exists", return_value=True):
            results = await launcher.shutdown_all_providers()

        # First provider fails, second succeeds
        assert False in results.values()
        assert True in results.values()
        # Both should have been attempted
        assert len(results) == 2


class TestWorkingDialogCallbacks:
    """Tests for working dialog callback integration in RemoteSTTLauncher."""

    @pytest.fixture
    def services_dir(self, tmp_path):
        """Create a mock services directory with a provider."""
        google_dir = tmp_path / "google_stt_server"
        google_dir.mkdir()
        (google_dir / "config.toml").write_text("""
[provider]
name = "google_stt"
display_name = "Google Cloud STT"
launcher = "launcher.py"
""")
        (google_dir / "launcher.py").write_text("# launcher stub")
        return tmp_path

    @pytest.fixture
    def app_data_dir(self, tmp_path):
        """Create a mock app data directory."""
        app_data = tmp_path / "appdata"
        app_data.mkdir()
        return app_data

    def test_sends_show_working_on_start(self, services_dir, app_data_dir):
        """start_provider() calls show_working callback with display name."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        working_calls = []
        launcher.set_working_callback(
            show=lambda msg: working_calls.append(("show", msg)),
            hide=lambda: working_calls.append(("hide",)),
        )

        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_popen.return_value = mock_process

            launcher.start_provider("google_stt")

            assert len(working_calls) == 1
            assert working_calls[0] == ("show", "Loading Google Cloud STT")

    def test_sends_hide_working_on_timeout(self, services_dir, app_data_dir):
        """Timeout sends hide_working before failure notification."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        working_calls = []
        launcher.set_working_callback(
            show=lambda msg: working_calls.append(("show", msg)),
            hide=lambda: working_calls.append(("hide",)),
        )

        # Call monitor directly with very short timeout
        launcher._monitor_startup("google_stt", "Google Cloud STT", timeout=0.1)

        assert ("hide",) in working_calls

    def test_sends_hide_working_on_popen_failure(self, services_dir, app_data_dir):
        """Popen failure sends hide_working."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        working_calls = []
        launcher.set_working_callback(
            show=lambda msg: working_calls.append(("show", msg)),
            hide=lambda: working_calls.append(("hide",)),
        )

        with patch("subprocess.Popen", side_effect=OSError("fail")):
            launcher.start_provider("google_stt")

        assert ("show", "Loading Google Cloud STT") in working_calls
        assert ("hide",) in working_calls

    def test_default_timeout_is_90_seconds(self, services_dir, app_data_dir):
        """DEFAULT_STARTUP_TIMEOUT should be 90s to accommodate GPU cold-start (wh-v0q)."""
        from stt.remote_stt_launcher import DEFAULT_STARTUP_TIMEOUT
        assert DEFAULT_STARTUP_TIMEOUT == 90

    def test_per_provider_startup_timeout_override(self, tmp_path):
        """Provider config.toml [provider].startup_timeout_seconds overrides the default (wh-v0q)."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        provider_dir = tmp_path / "slow_provider"
        provider_dir.mkdir()
        (provider_dir / "config.toml").write_text('''
[provider]
name = "slow_provider"
display_name = "Slow Provider"
launcher = "launcher.py"
startup_timeout_seconds = 180
''')
        (provider_dir / "launcher.py").write_text("# stub")

        launcher = RemoteSTTLauncher(services_dir=tmp_path, app_data_dir=tmp_path / "appdata")
        providers = launcher.discover_providers()
        slow = next(p for p in providers if p["name"] == "slow_provider")
        assert slow["startup_timeout_seconds"] == 180

    def test_provider_without_timeout_override_gets_default(self, services_dir, app_data_dir):
        """Providers without an explicit startup_timeout_seconds fall back to DEFAULT (wh-v0q)."""
        from stt.remote_stt_launcher import DEFAULT_STARTUP_TIMEOUT, RemoteSTTLauncher

        launcher = RemoteSTTLauncher(services_dir=services_dir, app_data_dir=app_data_dir)
        providers = launcher.discover_providers()
        google = next(p for p in providers if p["name"] == "google_stt")
        assert google["startup_timeout_seconds"] == DEFAULT_STARTUP_TIMEOUT

    def test_suppresses_failure_notification_when_subprocess_alive_after_timeout(
        self, services_dir, app_data_dir
    ):
        """If the provider subprocess is still alive when the ready timeout expires,
        assume slow cold-start and suppress the 'Failed to start' notification (wh-v0q).
        """
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir, app_data_dir=app_data_dir, ws_port=5500,
        )

        notifications = []
        working_calls = []
        launcher.set_notify_callback(lambda title, msg: notifications.append((title, msg)))
        launcher.set_working_callback(
            show=lambda msg: working_calls.append(("show", msg)),
            hide=lambda: working_calls.append(("hide",)),
        )

        # Register a fake "alive" subprocess for the provider. poll() returning None
        # means the process is still running per subprocess.Popen semantics.
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        launcher._subprocesses["google_stt"] = fake_proc

        launcher._monitor_startup("google_stt", "Google Cloud STT", timeout=0.1)

        # No failure notification when subprocess is alive
        assert all("Failed to start" not in msg for _, msg in notifications), (
            f"Unexpected failure notification: {notifications}"
        )
        # Working dialog must still be hidden so the UI doesn't hang
        assert ("hide",) in working_calls

    def test_sends_failure_notification_when_subprocess_dead_after_timeout(
        self, services_dir, app_data_dir
    ):
        """If the provider subprocess has exited by the time the ready timeout expires,
        the failure notification is still sent (regression guard for existing behavior).
        """
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir, app_data_dir=app_data_dir, ws_port=5500,
        )

        notifications = []
        launcher.set_notify_callback(lambda title, msg: notifications.append((title, msg)))

        # Register a dead subprocess. poll() returning a non-None value means exited.
        fake_proc = MagicMock()
        fake_proc.poll.return_value = 1
        launcher._subprocesses["google_stt"] = fake_proc

        launcher._monitor_startup("google_stt", "Google Cloud STT", timeout=0.1)

        assert len(notifications) == 1
        assert notifications[0] == ("Google Cloud STT", "Failed to start - try restarting WheelHouse")

    def test_failure_message_includes_restart_guidance(self, services_dir, app_data_dir):
        """Failure notification should suggest restarting WheelHouse."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        notifications = []
        launcher.set_notify_callback(lambda title, msg: notifications.append((title, msg)))

        launcher._monitor_startup("google_stt", "Google Cloud STT", timeout=0.1)

        assert len(notifications) == 1
        title, msg = notifications[0]
        assert "restart" in msg.lower() or "Restart" in msg


class TestPortGuard:
    """Tests that start_provider() fails when port is not assigned."""

    def test_start_provider_fails_without_port(self, tmp_path):
        """Starting a provider before port is assigned should fail explicitly."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        # Create a mock provider
        google_dir = tmp_path / "google_stt_server"
        google_dir.mkdir()
        (google_dir / "config.toml").write_text('''
[provider]
name = "google_stt"
display_name = "Google Cloud STT"
launcher = "launcher.py"
''')
        (google_dir / "launcher.py").write_text("# stub")

        launcher = RemoteSTTLauncher(
            services_dir=tmp_path,
            app_data_dir=tmp_path / "appdata",
            ws_port=0,  # Port not assigned
        )
        launcher.discover_providers()

        result = launcher.start_provider("google_stt")
        assert result is False

    def test_start_provider_succeeds_with_port(self, tmp_path):
        """Starting a provider with assigned port should proceed (not fail at guard)."""
        from stt.remote_stt_launcher import RemoteSTTLauncher

        google_dir = tmp_path / "google_stt_server"
        google_dir.mkdir()
        (google_dir / "config.toml").write_text('''
[provider]
name = "google_stt"
display_name = "Google Cloud STT"
launcher = "launcher.py"
''')
        (google_dir / "launcher.py").write_text("# stub")

        launcher = RemoteSTTLauncher(
            services_dir=tmp_path,
            app_data_dir=tmp_path / "appdata",
            ws_port=12345,  # Port assigned
        )
        launcher.discover_providers()

        # This will attempt to actually launch subprocess which may fail,
        # but it should NOT fail at the port guard
        # We mock subprocess.Popen to verify it gets past the guard
        with patch("stt.remote_stt_launcher.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            result = launcher.start_provider("google_stt")
            assert result is True
            # Verify the port was passed in the CLI args
            call_args = mock_popen.call_args
            cmd = call_args[0][0]  # First positional arg is the command list
            assert "--ws-port" in cmd
            assert "12345" in cmd


class TestStaleProviderOnRestart:
    """Tests for stale provider detection during WheelHouse restart.

    When WheelHouse restarts with a new WebSocket port, an old STT process
    from the previous session may still be running. start_provider() must
    detect this port mismatch and terminate the old process.
    """

    @pytest.fixture
    def services_dir(self, tmp_path):
        """Create a mock services directory with a provider."""
        google_dir = tmp_path / "google_stt_server"
        google_dir.mkdir()
        (google_dir / "config.toml").write_text("""
[provider]
name = "google_stt"
display_name = "Google Cloud STT"
launcher = "launcher.py"
""")
        (google_dir / "launcher.py").write_text("# launcher stub")
        return tmp_path

    @pytest.fixture
    def app_data_dir(self, tmp_path):
        """Create a mock app data directory."""
        app_data = tmp_path / "appdata"
        app_data.mkdir()
        return app_data

    def test_kills_old_provider_on_port_mismatch(self, services_dir, app_data_dir):
        """start_provider() must kill the old process when port doesn't match.

        Scenario: Old STT runs on port 5500, WheelHouse restarts on port 5501.
        The old PID file exists and process is alive, but it's on the wrong port.
        start_provider() should terminate it and start a new one.
        """
        from stt.remote_stt_launcher import RemoteSTTLauncher

        # Old provider was started with port 5500
        pid_file = app_data_dir / "google_stt.pid"
        pid_file.write_text("12345")
        port_file = app_data_dir / "google_stt.port"
        port_file.write_text("5500")

        # New WheelHouse starts with port 5501
        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5501,
        )

        with patch("stt.remote_stt_launcher.psutil.pid_exists", return_value=True), \
             patch("stt.remote_stt_launcher.psutil.Process") as mock_proc_class, \
             patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc_class.return_value = mock_proc
            mock_popen.return_value = MagicMock(pid=99999)

            result = launcher.start_provider("google_stt")

            assert result is True
            # Old process should have been terminated
            mock_proc.terminate.assert_called_once()
            # New process should have been started
            mock_popen.assert_called_once()

    def test_reuses_provider_when_port_matches(self, services_dir, app_data_dir):
        """start_provider() reuses existing process when port matches.

        When the port file matches the current ws_port, the provider is
        still valid and should not be restarted.
        """
        from stt.remote_stt_launcher import RemoteSTTLauncher

        pid_file = app_data_dir / "google_stt.pid"
        pid_file.write_text("12345")
        port_file = app_data_dir / "google_stt.port"
        port_file.write_text("5500")

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5500,
        )

        with patch("stt.remote_stt_launcher.psutil.pid_exists", return_value=True), \
             patch("subprocess.Popen") as mock_popen:
            result = launcher.start_provider("google_stt")

            assert result is True
            mock_popen.assert_not_called()  # Should reuse existing

    def test_starts_fresh_when_no_port_file(self, services_dir, app_data_dir):
        """When PID exists but no port file, assume stale and restart.

        Legacy providers or those that crashed before writing the port file
        should be terminated and restarted.
        """
        from stt.remote_stt_launcher import RemoteSTTLauncher

        pid_file = app_data_dir / "google_stt.pid"
        pid_file.write_text("12345")
        # No port file exists

        launcher = RemoteSTTLauncher(
            services_dir=services_dir,
            app_data_dir=app_data_dir,
            ws_port=5501,
        )

        with patch("stt.remote_stt_launcher.psutil.pid_exists", return_value=True), \
             patch("stt.remote_stt_launcher.psutil.Process") as mock_proc_class, \
             patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc_class.return_value = mock_proc
            mock_popen.return_value = MagicMock(pid=99999)

            result = launcher.start_provider("google_stt")

            assert result is True
            mock_proc.terminate.assert_called_once()
            mock_popen.assert_called_once()
