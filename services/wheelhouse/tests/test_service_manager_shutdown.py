"""Tests for ServiceManager shutdown behavior - T7: Shutdown on exit.

TDD: These tests are written FIRST per CLAUDE.md requirements.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directories to path for imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestShutdownServicesRemoteSTT:
    """Tests for shutdown_services() calling shutdown_all_providers()."""

    @pytest.fixture
    def mock_dependencies(self):
        """Create mock dependencies for ServiceManager."""
        config_service = MagicMock()
        config_service.get.return_value = None
        config_service.get_config.return_value = {}

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

    @pytest.mark.asyncio
    async def test_shutdown_calls_shutdown_all_providers(self, mock_dependencies):
        """shutdown_services() calls shutdown_all_providers on remote_stt_launcher."""
        from service_manager import ServiceManager

        service_manager = ServiceManager(**mock_dependencies)

        # Create a mock RemoteSTTLauncher with shutdown_all_providers
        mock_launcher = MagicMock()
        mock_launcher.shutdown_all_providers = AsyncMock(return_value={"google_stt": True})
        service_manager.remote_stt_launcher = mock_launcher

        await service_manager.shutdown_services()

        # Verify shutdown_all_providers was called
        mock_launcher.shutdown_all_providers.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_works_without_remote_stt_launcher(self, mock_dependencies):
        """shutdown_services() works when remote_stt_launcher is None."""
        from service_manager import ServiceManager

        service_manager = ServiceManager(**mock_dependencies)
        service_manager.remote_stt_launcher = None

        # Should not raise
        await service_manager.shutdown_services()

    @pytest.mark.asyncio
    async def test_shutdown_continues_if_provider_shutdown_fails(self, mock_dependencies):
        """shutdown_services() continues even if shutdown_all_providers raises."""
        from service_manager import ServiceManager

        service_manager = ServiceManager(**mock_dependencies)

        # Create a mock launcher that raises an exception
        mock_launcher = MagicMock()
        mock_launcher.shutdown_all_providers = AsyncMock(
            side_effect=Exception("WebSocket error")
        )
        service_manager.remote_stt_launcher = mock_launcher

        # Should not raise - shutdown should continue despite error
        await service_manager.shutdown_services()

    @pytest.mark.asyncio
    async def test_shutdown_logs_provider_results(self, mock_dependencies):
        """shutdown_services() logs the results of shutdown_all_providers."""
        from service_manager import ServiceManager

        service_manager = ServiceManager(**mock_dependencies)

        mock_launcher = MagicMock()
        mock_launcher.shutdown_all_providers = AsyncMock(
            return_value={"google_stt": True, "zipformer": False}
        )
        service_manager.remote_stt_launcher = mock_launcher

        with patch("service_manager.log") as mock_log:
            await service_manager.shutdown_services()

            # Should log the shutdown results
            log_calls = [str(call) for call in mock_log.info.call_args_list]
            # At least one log call should mention shutdown
            assert any("shutdown" in call.lower() or "stt" in call.lower()
                      for call in log_calls)
