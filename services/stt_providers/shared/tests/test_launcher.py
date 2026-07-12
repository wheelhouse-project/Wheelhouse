"""Tests for shared launcher restart behavior.

These tests verify that:
1. Exit code 0 prevents restart (clean shutdown)
2. Exit code non-zero with short uptime triggers restart (crash)
3. Restart flag triggers restart regardless of exit code
4. Constants can be configured
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestRestartDecision:
    """Tests for the should_restart decision logic."""

    def test_exit_code_zero_prevents_restart_even_with_short_uptime(self):
        """Exit code 0 should prevent restart regardless of uptime.

        When the provider receives a shutdown command, it exits with code 0.
        The launcher should NOT restart even if the process ran for less than
        the crash threshold time (15s).
        """
        from shared_stt.launcher import should_restart

        # Simulate exit after 2 seconds with exit code 0
        result = should_restart(
            exit_code=0,
            uptime=2.0,
            restart_flag_exists=False,
            crash_threshold_s=15.0
        )

        assert not result, "Exit code 0 should prevent restart even with short uptime"

    def test_nonzero_exit_with_short_uptime_triggers_restart(self):
        """Non-zero exit code with short uptime should trigger restart (crash).

        When the process crashes (non-zero exit code) within the crash threshold,
        the launcher should restart it.
        """
        from shared_stt.launcher import should_restart

        # Simulate crash after 2 seconds
        result = should_restart(
            exit_code=1,
            uptime=2.0,
            restart_flag_exists=False,
            crash_threshold_s=15.0
        )

        assert result, "Non-zero exit with short uptime should trigger restart"

    def test_restart_flag_takes_precedence(self):
        """Restart flag should trigger restart regardless of exit code.

        When the restart flag file exists, the launcher should restart
        even if the exit code was 0.
        """
        from shared_stt.launcher import should_restart

        # Simulate intentional restart request
        result = should_restart(
            exit_code=0,
            uptime=5.0,
            restart_flag_exists=True,
            crash_threshold_s=15.0
        )

        assert result, "Restart flag should trigger restart even with exit code 0"

    def test_long_uptime_nonzero_exit_no_restart(self):
        """Long uptime with non-zero exit should NOT trigger restart.

        If the process ran for longer than the crash threshold and then
        exits with non-zero code, it's not considered a crash.
        """
        from shared_stt.launcher import should_restart

        result = should_restart(
            exit_code=1,
            uptime=60.0,  # Ran for 60 seconds
            restart_flag_exists=False,
            crash_threshold_s=15.0
        )

        assert not result, "Long uptime with non-zero exit should not restart"


class TestLauncherConfig:
    """Tests for launcher configuration."""

    def test_launcher_config_defaults(self):
        """LauncherConfig should have sensible defaults."""
        from shared_stt.launcher import LauncherConfig

        config = LauncherConfig(app_name="TestApp", main_script="main.py")

        assert config.app_name == "TestApp"
        assert config.main_script == "main.py"
        assert config.crash_threshold_s == 15
        assert config.max_crashes == 3

    def test_launcher_config_custom_values(self):
        """LauncherConfig should accept custom values."""
        from shared_stt.launcher import LauncherConfig

        config = LauncherConfig(
            app_name="CustomApp",
            main_script="custom_main.py",
            crash_threshold_s=30,
            max_crashes=5
        )

        assert config.crash_threshold_s == 30
        assert config.max_crashes == 5


class TestPidFilePath:
    """Tests for PID file path generation."""

    def test_pid_file_path_windows(self):
        """PID file should be in APPDATA on Windows."""
        from shared_stt.launcher import get_pid_file_path

        with patch('shared_stt.launcher.sys.platform', 'win32'):
            with patch.dict(os.environ, {'APPDATA': 'C:\\Users\\Test\\AppData\\Roaming'}):
                with patch('os.makedirs'):  # Don't actually create dirs
                    path = get_pid_file_path("TestApp")
                    assert "WheelHouse" in path
                    assert "testapp.pid" in path.lower()

    def test_pid_file_path_linux(self):
        """PID file should be in ~/.wheelhouse on Linux."""
        from shared_stt.launcher import get_pid_file_path

        with patch('shared_stt.launcher.sys.platform', 'linux'):
            with patch('shared_stt.launcher.os.path.expanduser', return_value='/home/user/.wheelhouse'):
                with patch('os.makedirs'):  # Don't actually create dirs
                    path = get_pid_file_path("TestApp")
                    assert ".wheelhouse" in path or "WheelHouse" in path


class TestRestartFlagPath:
    """Tests for restart flag path generation."""

    def test_restart_flag_path(self):
        """Restart flag path should be in app data directory."""
        from shared_stt.launcher import get_restart_flag_path

        path = get_restart_flag_path("TestApp")
        assert "testapp.restart" in path.lower()
