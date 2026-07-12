"""Tests for T8: UI provider switching.

Tests for the UI provider switching feature that allows users to switch
between STT providers via system tray and floating button menus.

TDD: These tests are written FIRST per CLAUDE.md requirements.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from queue import Queue

import pytest

# wh-pytest-flaky-segfault: these tests construct GuiManager, which
# builds real Qt widgets; without a QApplication Qt aborts the whole
# interpreter (no traceback, output lost). The session-scoped qapp
# fixture guarantees one exists even when this file runs in isolation.
pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")

# Add parent directories to path for imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestStateManagerProviderDiscovery:
    """Tests for StateManager providing discovered providers to GUI."""

    @pytest.fixture
    def mock_remote_stt_launcher(self):
        """Create mock RemoteSTTLauncher with discovered providers."""
        launcher = MagicMock()
        providers = [
            {
                "name": "google_stt",
                "display_name": "Google Cloud STT",
                "launcher": "launcher.py",
                "service_dir": Path("/mock/google_stt_server"),
            },
            {
                "name": "zipformer",
                "display_name": "Zipformer (Local)",
                "launcher": "launcher.py",
                "service_dir": Path("/mock/sherpa_zipformer"),
            },
        ]
        launcher.get_providers.return_value = providers
        launcher.discover_providers.return_value = providers
        launcher.get_provider_by_name.side_effect = lambda name: next(
            (p for p in providers if p["name"] == name), None
        )
        return launcher

    @pytest.fixture
    def mock_config_service(self):
        """Create mock ConfigService for remote mode."""
        config_service = MagicMock()
        config_service.get.side_effect = lambda key, default=None: {
            "stt.mode": "remote",
            "stt.last_provider": "google_stt",
        }.get(key, default)
        return config_service

    @pytest.fixture
    def state_manager(self, mock_config_service):
        """Create StateManager with mocked dependencies."""
        from state_manager import StateManager

        event_bus = MagicMock()
        loop = asyncio.new_event_loop()
        state_to_gui_queue = Queue()
        websocket_manager = None

        sm = StateManager(
            config_service=mock_config_service,
            event_bus=event_bus,
            loop=loop,
            state_to_gui_queue=state_to_gui_queue,
            websocket_manager=websocket_manager,
        )
        yield sm
        loop.close()

    def test_get_available_providers_uses_launcher_in_remote_mode(
        self, state_manager, mock_remote_stt_launcher, mock_config_service
    ):
        """In remote mode, _get_available_stt_providers() uses RemoteSTTLauncher."""
        # Setup: inject the launcher
        state_manager.set_remote_stt_launcher(mock_remote_stt_launcher)

        # Act
        providers = state_manager._get_available_stt_providers()

        # Assert: should return provider names from discover_providers
        # Note: zipformer is expanded into CPU and GPU variants
        assert "google_stt" in providers
        assert "zipformer_cpu" in providers
        assert "zipformer_gpu" in providers
        # Should NOT return hardcoded names like "google_remote"
        assert "google_remote" not in providers

    def test_get_current_provider_returns_actual_name_in_remote_mode(
        self, state_manager, mock_remote_stt_launcher, mock_config_service
    ):
        """In remote mode, _get_current_stt_provider() returns actual provider name."""
        state_manager.set_remote_stt_launcher(mock_remote_stt_launcher)

        # Act
        current = state_manager._get_current_stt_provider()

        # Assert: should return actual provider name from config
        assert current == "google_stt"
        # Should NOT return "google_remote"
        assert current != "google_remote"

    def test_state_update_includes_provider_display_names(
        self, state_manager, mock_remote_stt_launcher, mock_config_service
    ):
        """send_state_update() includes provider info with display names."""
        state_manager.set_remote_stt_launcher(mock_remote_stt_launcher)

        # Act
        state_manager.send_state_update()

        # Get the message from queue
        msg = state_manager.state_to_gui_queue.get_nowait()

        # Assert: should have both provider info
        assert "stt_provider" in msg
        assert "stt_providers_available" in msg
        assert msg["stt_provider"] == "google_stt"
        assert "google_stt" in msg["stt_providers_available"]
        # Note: zipformer is expanded into CPU and GPU variants
        assert "zipformer_cpu" in msg["stt_providers_available"]
        assert "zipformer_gpu" in msg["stt_providers_available"]

        # Assert: should include display name mapping
        assert "stt_provider_display_names" in msg
        assert msg["stt_provider_display_names"]["google_stt"] == "Google Cloud STT"
        assert msg["stt_provider_display_names"]["zipformer_cpu"] == "Zipformer CPU"
        assert msg["stt_provider_display_names"]["zipformer_gpu"] == "Zipformer GPU"


class TestRemoteProviderSwitching:
    """Tests for switching between remote providers in main.py."""

    @pytest.fixture
    def mock_dependencies(self):
        """Create mock dependencies for LogicController."""
        config_service = MagicMock()
        config_service.get.side_effect = lambda key, default=None: {
            "stt.mode": "remote",
            "stt.last_provider": "google_stt",
        }.get(key, default)
        config_service.set = MagicMock()
        config_service.save = AsyncMock()

        remote_launcher = MagicMock()
        remote_launcher.stop_provider = AsyncMock(return_value=True)
        remote_launcher.start_provider = MagicMock(return_value=True)
        remote_launcher.get_provider_by_name.return_value = {
            "name": "zipformer",
            "display_name": "Zipformer (Local)",
        }

        service_manager = MagicMock()
        service_manager.remote_stt_launcher = remote_launcher

        state_manager = MagicMock()
        state_manager.state_to_gui_queue = Queue()
        state_manager.send_state_update = MagicMock()

        return {
            "config_service": config_service,
            "service_manager": service_manager,
            "state_manager": state_manager,
            "remote_launcher": remote_launcher,
        }

    @pytest.mark.asyncio
    async def test_switch_between_remote_providers_stops_old_starts_new(
        self, mock_dependencies
    ):
        """Switching remote providers stops current provider and starts new one."""
        from main import LogicController

        config_service = mock_dependencies["config_service"]
        service_manager = mock_dependencies["service_manager"]
        state_manager = mock_dependencies["state_manager"]
        remote_launcher = mock_dependencies["remote_launcher"]

        # Create controller with mocked dependencies
        controller = MagicMock(spec=LogicController)
        controller.config_service = config_service
        controller.service_manager = service_manager
        controller.state_manager = state_manager
        controller.shutdown_event = MagicMock()
        controller.shutdown_event.is_set.return_value = False

        # Import and call the actual method
        from main import LogicController as LC

        # Bind the actual method to our mock controller
        switch_method = LC._switch_stt_provider
        await switch_method(controller, "zipformer")

        # Assert: should stop current provider
        remote_launcher.stop_provider.assert_called_once_with("google_stt")

        # Assert: should start new provider
        remote_launcher.start_provider.assert_called_once_with("zipformer")

        # Assert: should update config
        config_service.set.assert_any_call("stt.last_provider", "zipformer")
        config_service.save.assert_called()

        # Assert: should notify GUI
        state_manager.send_state_update.assert_called()

    @pytest.mark.asyncio
    async def test_switch_to_same_provider_is_noop(self, mock_dependencies):
        """Switching to the same provider does nothing."""
        from main import LogicController

        config_service = mock_dependencies["config_service"]
        service_manager = mock_dependencies["service_manager"]
        state_manager = mock_dependencies["state_manager"]
        remote_launcher = mock_dependencies["remote_launcher"]

        controller = MagicMock(spec=LogicController)
        controller.config_service = config_service
        controller.service_manager = service_manager
        controller.state_manager = state_manager
        controller.shutdown_event = MagicMock()
        controller.shutdown_event.is_set.return_value = False

        from main import LogicController as LC

        # Try to switch to same provider
        await LC._switch_stt_provider(controller, "google_stt")

        # Assert: should NOT stop or start providers
        remote_launcher.stop_provider.assert_not_called()
        remote_launcher.start_provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_switch_handles_stop_failure_gracefully(self, mock_dependencies):
        """If stopping old provider fails, still try to start new one."""
        from main import LogicController

        config_service = mock_dependencies["config_service"]
        service_manager = mock_dependencies["service_manager"]
        state_manager = mock_dependencies["state_manager"]
        remote_launcher = mock_dependencies["remote_launcher"]

        # Make stop fail
        remote_launcher.stop_provider = AsyncMock(return_value=False)

        controller = MagicMock(spec=LogicController)
        controller.config_service = config_service
        controller.service_manager = service_manager
        controller.state_manager = state_manager
        controller.shutdown_event = MagicMock()
        controller.shutdown_event.is_set.return_value = False

        from main import LogicController as LC

        # Should not raise, should continue to start new provider
        await LC._switch_stt_provider(controller, "zipformer")

        # Still should try to start new provider
        remote_launcher.start_provider.assert_called_once_with("zipformer")


class TestGuiProviderDisplay:
    """Tests for GUI displaying providers with correct names."""

    @pytest.fixture
    def gui_manager(self):
        """Create GuiManager with mocked dependencies."""
        from gui import GuiManager

        shutdown_event = MagicMock()
        commands_queue = Queue()
        state_queue = Queue()

        # Mock Qt components to avoid GUI initialization
        with patch("gui.QApplication"):
            with patch("gui.FloatingButton"):
                with patch("gui.pystray.Icon"):
                    with patch("gui.WorkingDialog"):
                        manager = GuiManager(
                            shutdown_event, commands_queue, state_queue
                        )
                        manager.initial_state_received = True
                        yield manager

    def test_gui_uses_display_names_from_state_update(self, gui_manager):
        """GUI should use display_names dict from state update."""
        # Simulate receiving state update with display names
        gui_manager.stt_providers_available = ["google_stt", "zipformer"]
        gui_manager.stt_provider = "google_stt"
        gui_manager.stt_provider_display_names = {
            "google_stt": "Google Cloud STT",
            "zipformer": "Zipformer (Local)",
        }

        # Get display name
        display_name = gui_manager._get_provider_display_name("google_stt")

        # Should use the display name from mapping
        assert display_name == "Google Cloud STT"

    def test_construction_builds_no_real_qt_dialogs(self, gui_manager):
        """GuiManager unit tests must not construct real QDialogs. Every
        incidental native widget is access-violation surface in full-suite
        runs: the 2026-07-05 night crashes both died inside
        WorkingDialog.__init__ during this file's gui_manager fixture
        (wh-pytest-flaky-segfault)."""
        from PySide6.QtWidgets import QDialog

        assert not isinstance(gui_manager._te_window, QDialog)
        assert not isinstance(gui_manager.working_dialog, QDialog)

    def test_gui_falls_back_to_title_case_if_no_display_name(self, gui_manager):
        """GUI should fall back to title case if display name not in mapping."""
        gui_manager.stt_provider_display_names = {}

        # Get display name for unknown provider
        display_name = gui_manager._get_provider_display_name("unknown_provider")

        # Should fall back to title case with underscores replaced by spaces
        assert display_name == "Unknown Provider"


class TestProviderSwitchCommand:
    """Tests for the switch_stt_provider GUI command."""

    def test_gui_sends_switch_command_with_provider_name(self):
        """GUI switch_stt_provider() sends command with provider name."""
        from gui import GuiManager

        shutdown_event = MagicMock()
        commands_queue = Queue()
        state_queue = Queue()

        with patch("gui.QApplication"):
            with patch("gui.FloatingButton"):
                with patch("gui.pystray.Icon"):
                    with patch("gui.WorkingDialog"):
                        manager = GuiManager(
                            shutdown_event, commands_queue, state_queue
                        )

        # Call switch
        manager.switch_stt_provider("zipformer")

        # Check queue
        cmd = commands_queue.get_nowait()
        assert cmd["action"] == "switch_stt_provider"
        assert cmd["provider"] == "zipformer"
