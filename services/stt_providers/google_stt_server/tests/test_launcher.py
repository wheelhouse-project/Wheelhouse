"""Tests for launcher restart behavior.

These tests verify that:
1. Exit code 0 prevents restart (clean shutdown)
2. Exit code non-zero with short uptime triggers restart (crash)
3. Restart flag triggers restart regardless of exit code
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestRestartBehavior:
    """Tests for restart decision logic in launcher."""

    def test_exit_code_zero_prevents_restart_even_with_short_uptime(self):
        """Exit code 0 should prevent restart regardless of uptime.

        When the provider receives a shutdown command, it exits with code 0.
        The launcher should NOT restart even if the process ran for less than
        the crash threshold time (15s).
        """
        # Import the shared launcher's should_restart function
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
