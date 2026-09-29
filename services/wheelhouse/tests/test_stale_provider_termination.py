"""Stale provider cleanup stops only the provider process
(wh-stale-provider-pid-reuse).

When WheelHouse starts and finds that a provider's stored port differs from
the current port, start_provider() calls _terminate_stale_provider() to stop the
provider left over from an earlier session. The PID file that names that
provider can outlive it (an unclean exit, a Windows restart), and Windows
reuses process ids, so the id in the file can name an unrelated program.
The function stops the process only when _pid_file_names_live_provider()
says the file names a live provider; the PID file and the port file are
removed in every case. psutil is faked: no real process is stopped.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

PROVIDER = "parakeet_tdt"
_WRITTEN = 1_000_000.0  # the PID file's modification time in these tests
_PID = 424242


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    """A launcher whose app data folder holds a PID file and a port file."""
    from stt.remote_stt_launcher import RemoteSTTLauncher

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    services = tmp_path / "stt_providers"
    services.mkdir()
    launcher = RemoteSTTLauncher(services_dir=services,
                                 app_data_dir=tmp_path / "appdata")
    launcher.app_data_dir.mkdir(parents=True, exist_ok=True)
    pid_file = launcher.app_data_dir / f"{PROVIDER}.pid"
    pid_file.write_text(str(_PID))
    os.utime(pid_file, (_WRITTEN, _WRITTEN))
    (launcher.app_data_dir / f"{PROVIDER}.port").write_text("50051")
    return launcher


def _process(monkeypatch, *, create_time=None, create_raises=None,
             name="python.exe"):
    """Stand in psutil.Process for the id in the PID file; it exists."""
    monkeypatch.setattr("stt.remote_stt_launcher.psutil.pid_exists",
                        lambda pid: pid == _PID)
    process = MagicMock()
    if create_raises is not None:
        process.create_time.side_effect = create_raises
    else:
        process.create_time.return_value = create_time
    process.name.return_value = name
    monkeypatch.setattr("stt.remote_stt_launcher.psutil.Process",
                        lambda pid: process)
    return process


def _files_removed(launcher) -> bool:
    return not any(
        (launcher.app_data_dir / f"{PROVIDER}{suffix}").exists()
        for suffix in (".pid", ".port")
    )


def _not_stopped_lines(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.INFO and PROVIDER in r.getMessage()
        and str(_PID) in r.getMessage() and "not stop" in r.getMessage()
    ]


class TestTerminateStaleProvider:
    def test_a_process_newer_than_the_pid_file_is_not_stopped(
        self, launcher, monkeypatch, caplog
    ):
        process = _process(monkeypatch, create_time=_WRITTEN + 3.0)

        with caplog.at_level(logging.INFO, logger="stt.remote_stt_launcher"):
            launcher._terminate_stale_provider(PROVIDER)

        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        assert len(_not_stopped_lines(caplog)) == 1
        assert _files_removed(launcher)

    def test_a_process_older_than_the_pid_file_is_stopped(
        self, launcher, monkeypatch
    ):
        process = _process(monkeypatch, create_time=_WRITTEN - 60.0)

        launcher._terminate_stale_provider(PROVIDER)

        process.terminate.assert_called_once_with()
        assert _files_removed(launcher)

    def test_access_denied_and_another_program_name_is_not_stopped(
        self, launcher, monkeypatch, caplog
    ):
        import psutil

        process = _process(monkeypatch,
                           create_raises=psutil.AccessDenied(_PID),
                           name="notepad.exe")

        with caplog.at_level(logging.INFO, logger="stt.remote_stt_launcher"):
            launcher._terminate_stale_provider(PROVIDER)

        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        assert len(_not_stopped_lines(caplog)) == 1
        assert _files_removed(launcher)

    def test_access_denied_and_a_provider_name_is_stopped(
        self, launcher, monkeypatch
    ):
        import psutil

        process = _process(monkeypatch,
                           create_raises=psutil.AccessDenied(_PID),
                           name="python.exe")

        launcher._terminate_stale_provider(PROVIDER)

        process.terminate.assert_called_once_with()
        assert _files_removed(launcher)
