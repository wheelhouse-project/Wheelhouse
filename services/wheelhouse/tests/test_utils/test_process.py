"""Tests for process.py - PID file management and single-instance enforcement.

Tests cover:
- get_pid_file_path returns correct path
- write_pid_file writes current PID
- clear_pid_file removes PID file
- manage_process_instance handles stale/active instances
"""

import os
from unittest.mock import Mock, patch, MagicMock

import pytest


class TestGetPidFilePath:
    """Tests for get_pid_file_path function."""

    def test_returns_path_under_appdata(self, tmp_path):
        from utils.process import get_pid_file_path

        appdata = str(tmp_path / "AppData" / "Roaming")
        with patch.dict(os.environ, {"APPDATA": appdata}):
            path = get_pid_file_path()

        assert "WheelHouse" in path
        assert path.endswith("wheelhouse.pid")

    def test_uses_home_when_no_appdata(self):
        from utils.process import get_pid_file_path

        with patch.dict(os.environ, {}, clear=True):
            with patch("utils.process.os.getenv", return_value=None):
                with patch("utils.process.os.path.expanduser", return_value="/home/test"):
                    with patch("utils.process.os.makedirs"):
                        path = get_pid_file_path()

        assert path.endswith("wheelhouse.pid")


class TestWritePidFile:
    """Tests for write_pid_file function."""

    def test_writes_current_pid(self, tmp_path):
        from utils.process import write_pid_file

        pid_file = str(tmp_path / "wheelhouse.pid")
        with patch("utils.process.get_pid_file_path", return_value=pid_file):
            write_pid_file()

        content = open(pid_file).read()
        assert content == str(os.getpid())

    def test_io_error_exits(self, tmp_path):
        from utils.process import write_pid_file

        with patch("utils.process.get_pid_file_path", return_value="/nonexistent/path/pid"):
            with patch("builtins.open", side_effect=IOError("permission denied")):
                with pytest.raises(SystemExit):
                    write_pid_file()


class TestClearPidFile:
    """Tests for clear_pid_file function."""

    def test_removes_existing_file(self, tmp_path):
        from utils.process import clear_pid_file

        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("12345")

        with patch("utils.process.get_pid_file_path", return_value=str(pid_file)):
            clear_pid_file()

        assert not pid_file.exists()

    def test_no_file_no_error(self, tmp_path):
        from utils.process import clear_pid_file

        pid_file = str(tmp_path / "nonexistent.pid")
        with patch("utils.process.get_pid_file_path", return_value=pid_file):
            # Should not raise
            clear_pid_file()

    def test_io_error_logged_not_raised(self, tmp_path):
        from utils.process import clear_pid_file

        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("12345")

        with patch("utils.process.get_pid_file_path", return_value=str(pid_file)):
            with patch("utils.process.os.remove", side_effect=IOError("busy")):
                # Should not raise
                clear_pid_file()


class TestManageProcessInstance:
    """Tests for manage_process_instance function."""

    def test_no_pid_file_is_clean_start(self, tmp_path):
        from utils.process import manage_process_instance

        pid_file = str(tmp_path / "nonexistent.pid")
        with patch("utils.process.get_pid_file_path", return_value=pid_file):
            # Should return normally
            manage_process_instance()

    def test_stale_pid_file_cleaned_up(self, tmp_path):
        from utils.process import manage_process_instance

        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("99999")

        with patch("utils.process.get_pid_file_path", return_value=str(pid_file)):
            with patch("utils.process.psutil.pid_exists", return_value=False):
                manage_process_instance()

        # PID file should be cleaned up
        assert not pid_file.exists()

    def test_own_pid_in_file_ignored(self, tmp_path):
        from utils.process import manage_process_instance

        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text(str(os.getpid()))

        with patch("utils.process.get_pid_file_path", return_value=str(pid_file)):
            # Should return without terminating self
            manage_process_instance()

    def test_active_old_instance_terminated(self, tmp_path):
        from utils.process import manage_process_instance

        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("12345")

        mock_parent = Mock()
        mock_child = Mock()
        mock_parent.children.return_value = [mock_child]

        with patch("utils.process.get_pid_file_path", return_value=str(pid_file)):
            with patch("utils.process.psutil.pid_exists", return_value=True):
                with patch("utils.process.psutil.Process", return_value=mock_parent):
                    with patch("utils.process.psutil.wait_procs", return_value=([], [])):
                        manage_process_instance()

        mock_child.terminate.assert_called_once()
        mock_parent.terminate.assert_called_once()

    def test_stubborn_process_killed(self, tmp_path):
        from utils.process import manage_process_instance

        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("12345")

        mock_parent = Mock()
        mock_parent.children.return_value = []
        mock_parent.pid = 12345

        with patch("utils.process.get_pid_file_path", return_value=str(pid_file)):
            with patch("utils.process.psutil.pid_exists", return_value=True):
                with patch("utils.process.psutil.Process", return_value=mock_parent):
                    # Process doesn't terminate gracefully
                    with patch("utils.process.psutil.wait_procs", return_value=([], [mock_parent])):
                        manage_process_instance()

        mock_parent.kill.assert_called_once()

    def test_corrupt_pid_file_handled(self, tmp_path):
        from utils.process import manage_process_instance

        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("not_a_number")

        with patch("utils.process.get_pid_file_path", return_value=str(pid_file)):
            # Should not raise
            manage_process_instance()
