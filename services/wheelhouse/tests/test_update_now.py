"""Update now: the installer run (wh-release-update-check, acceptance item 6).

update_install.py prepares the file and the parameters (test_update_install.py).
This file covers the second half:

  * the seam in stt.parakeet_model.build_launch_command and launch_installer
    that carries a list of installer parameters, and the pin that holds the
    command text of the Parakeet caller unchanged;
  * LogicController._handle_update_now: download and check first, then the
    wait-for-exit console, then the ordinary shutdown; any failure writes one
    log line and starts nothing.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import MagicMock, patch

import pytest

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import update_install  # noqa: E402
from stt import parakeet_model as pm  # noqa: E402
from update_check import UpdateCheckError  # noqa: E402

SCRIPT = Path(r"C:\Users\O'Brien\AppData\Local\WheelhouseSetup\install-wheelhouse.ps1")
VERSION = "1.2.3"
TAG = "v1.2.3"

# The wrapper that build_launch_command made for SCRIPT, [111, 222] and
# auto_start=True before the seam existed, copied once from the old code.
# The Parakeet caller must keep this text to the character.
GOLDEN_PARAKEET_WRAPPER = "$ids = @(111,222); $limit = 120; $deadline = (Get-Date).AddSeconds($limit); $appDir = Join-Path $env:LOCALAPPDATA 'Wheelhouse\\app'; $pattern = [regex]::Escape($appDir.ToLower()) + '($|[\\\\/' + [char]34 + '''\\s])'; function Get-WheelhouseCount { $alive = @(); if ($ids.Count -gt 0) { $alive = @(Get-Process -Id $ids -ErrorAction SilentlyContinue) }; try { $procs = @(Get-CimInstance Win32_Process -Filter 'Name = ''python.exe'' OR Name = ''pythonw.exe'' OR Name = ''uv.exe''' -ErrorAction Stop) } catch { return $alive.Count }; return $alive.Count + @($procs | Where-Object { $_.CommandLine -and ($_.CommandLine.ToLower() -match $pattern) }).Count }; Write-Host 'Waiting for Wheelhouse to close.'; if ($ids.Count -gt 0) { Wait-Process -Id $ids -Timeout $limit -ErrorAction SilentlyContinue }; while (((Get-WheelhouseCount) -gt 0) -and ((Get-Date) -lt $deadline)) { Start-Sleep -Seconds 1 }; if ((Get-WheelhouseCount) -gt 0) { Write-Host 'Wheelhouse did not close within 120 seconds, so the Wheelhouse installer was not started.'; Write-Host 'Exit Wheelhouse (right-click the Wheelhouse tray icon and choose Exit), then choose Parakeet again.'; Write-Host 'Press any key to close this window.'; try { $null = [Console]::ReadKey($true) } catch { }; exit 1 }; Write-Host 'Starting the Wheelhouse installer.'; & 'C:\\Users\\O''Brien\\AppData\\Local\\WheelhouseSetup\\install-wheelhouse.ps1' -SttProvider parakeet_tdt -StartNow yes -AutoStart yes; Write-Host ''; Write-Host 'The Wheelhouse installer has stopped. Press any key to close this window.'; try { $null = [Console]::ReadKey($true) } catch { }; exit 0"


@pytest.fixture(autouse=True)
def _system_root(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")


# --- The seam: the Parakeet command text does not change ----------------------


def test_the_parakeet_command_text_is_pinned_for_auto_start_yes():
    cmd = pm.build_launch_command(SCRIPT, [111, 222], auto_start=True)
    assert cmd[0] == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    assert cmd[1:5] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
    assert cmd[5] == GOLDEN_PARAKEET_WRAPPER


def test_the_parakeet_command_text_is_pinned_for_auto_start_no():
    assert GOLDEN_PARAKEET_WRAPPER.count("-AutoStart yes") == 1
    expected = GOLDEN_PARAKEET_WRAPPER.replace("-AutoStart yes", "-AutoStart no")
    assert pm.build_launch_command(SCRIPT, [111, 222], auto_start=False)[5] == expected


# --- The seam: a parameter list in place of the three fixed parameters --------


def test_a_parameter_list_replaces_the_three_fixed_parameters():
    params = ["-SttProvider", "google_stt", "-StartNow", "yes", "-AutoStart", "no"]
    wrapper = pm.build_launch_command(SCRIPT, [111, 222], installer_parameters=params)[5]
    expected = GOLDEN_PARAKEET_WRAPPER.replace(
        "-SttProvider parakeet_tdt -StartNow yes -AutoStart yes",
        "-SttProvider google_stt -StartNow yes -AutoStart no",
    ).replace(
        "then choose Parakeet again.", "then run the installer from the website."
    )
    assert wrapper == expected


def test_an_empty_parameter_list_runs_the_script_with_no_parameters():
    wrapper = pm.build_launch_command(SCRIPT, [1], installer_parameters=[])[5]
    assert "-SttProvider" not in wrapper
    assert "install-wheelhouse.ps1'; Write-Host" in wrapper


def test_the_console_names_the_right_way_to_try_again():
    # The console prints this instruction when Wheelhouse does not close in
    # time, because the installer then never starts. The Parakeet offer and
    # the update need different instructions. The Parakeet offer comes back
    # from the model list, so the user can choose Parakeet again. The update
    # notice closes when the user presses "Update now", and update_check.py
    # records the notice as shown, so the same notice does not come back for
    # seven days. Until the notice appears again, the website is therefore
    # the only way to try the update (wh-release-update-check.2 item 3).
    parakeet = pm.build_launch_command(SCRIPT, [111], auto_start=True)[5]
    assert "then choose Parakeet again." in parakeet
    assert "then run the installer from the website." not in parakeet
    update = pm.build_launch_command(
        SCRIPT, [111], installer_parameters=["-StartNow", "yes"]
    )[5]
    assert "then run the installer from the website." in update
    assert "then choose Parakeet again." not in update


def test_a_parameter_list_and_auto_start_together_are_refused():
    with pytest.raises(ValueError):
        pm.build_launch_command(SCRIPT, [1], auto_start=True, installer_parameters=[])


def test_neither_auto_start_nor_a_parameter_list_is_refused():
    with pytest.raises(ValueError):
        pm.build_launch_command(SCRIPT, [1])


@pytest.mark.parametrize(
    "bad",
    [
        ["-SttProvider", "x; Remove-Item y"],
        ["-SttProvider", "a b"],
        ["-StartNow yes"],
        ["-SttProvider", "it's"],
        ["-SttProvider", ""],
        ["-SttProvider", 'a"b'],
        [5],
    ],
)
def test_a_parameter_that_could_change_the_command_is_refused(bad):
    with pytest.raises((ValueError, TypeError)):
        pm.build_launch_command(SCRIPT, [1], installer_parameters=bad)


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return object()


def test_the_launcher_passes_the_parameter_list_and_does_not_read_the_shortcut(
    tmp_path, monkeypatch
):
    runner = _Recorder()
    monkeypatch.setattr(pm, "_popen", runner)
    monkeypatch.setattr(
        pm, "startup_shortcut_exists", MagicMock(side_effect=AssertionError("read"))
    )
    script = tmp_path / "install-wheelhouse.ps1"
    params = ["-SttProvider", "distil_medium_en", "-StartNow", "yes", "-AutoStart", "yes"]
    assert pm.launch_installer(script, [42], installer_parameters=params) is True
    (args, kwargs), = runner.calls
    assert args == pm.build_launch_command(script, [42], installer_parameters=params)
    assert "-SttProvider distil_medium_en" in args[5]


# --- _handle_update_now -----------------------------------------------------


def _controller(events):
    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller.config_service = MagicMock()
    controller.shutdown_event = None
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = Queue()
    controller._wheelhouse_process_ids.return_value = [11, 22]
    controller.request_shutdown.side_effect = lambda: events.append("shutdown")
    return controller


def _run(controller, version=VERSION, tag=TAG):
    from main import LogicController

    asyncio.run(LogicController._handle_update_now(controller, version, tag))


PARAMS = ["-SttProvider", "parakeet_tdt", "-StartNow", "yes", "-AutoStart", "no"]


def _patched(events, *, prepare=None, launch_result=True):
    prepared = update_install.PreparedUpdate(
        script_path=Path("C:/tmp/wheelhouse-update-x/install-wheelhouse.ps1"),
        tag=TAG,
        digest_checked=True,
    )

    def fake_prepare(tag, running_version):
        events.append("prepare")
        if prepare is not None:
            raise prepare
        return prepared

    def fake_launch(script_path, pids, **kwargs):
        events.append("launch")
        fake_launch.call = (script_path, list(pids), kwargs)
        return launch_result

    fake_launch.call = None
    return (
        patch("main.update_install.prepare_update", side_effect=fake_prepare),
        patch("main.update_install.build_installer_parameters", return_value=list(PARAMS)),
        patch("main.parakeet_model.launch_installer", side_effect=fake_launch),
        fake_launch,
        prepared,
    )


FAILURE_MESSAGE = {"action": "update_failed"}


def _gui_messages(controller):
    messages = []
    gui_queue = controller.state_manager.state_to_gui_queue
    while True:
        try:
            messages.append(gui_queue.get_nowait())
        except Empty:
            return messages


def _warnings(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "main" and r.levelno >= logging.WARNING
    ]


def test_the_file_is_ready_before_the_installer_starts_and_before_wheelhouse_closes():
    events = []
    controller = _controller(events)
    p_prepare, p_params, p_launch, fake_launch, prepared = _patched(events)
    with p_prepare, p_params, p_launch:
        _run(controller)
    assert events == ["prepare", "launch", "shutdown"]
    script_path, pids, kwargs = fake_launch.call
    assert script_path == prepared.script_path
    assert pids == [11, 22]
    assert kwargs == {"installer_parameters": PARAMS}
    assert _gui_messages(controller) == [], "no failure notice after a success"


def test_a_failure_to_build_the_parameters_starts_nothing_and_shows_the_notice(caplog):
    events = []
    controller = _controller(events)
    p_prepare, _, p_launch, _, _ = _patched(events)
    with p_prepare, p_launch, caplog.at_level(logging.INFO, logger="main"), patch(
        "main.update_install.build_installer_parameters", side_effect=OSError("config")
    ):
        _run(controller)
    assert events == ["prepare"]
    controller.request_shutdown.assert_not_called()
    assert _gui_messages(controller) == [FAILURE_MESSAGE]


def test_a_full_gui_queue_does_not_turn_a_failure_into_a_crash(caplog):
    events = []
    controller = _controller(events)
    controller.state_manager.state_to_gui_queue = Queue(maxsize=1)
    controller.state_manager.state_to_gui_queue.put_nowait("full")
    p_prepare, p_params, p_launch, _, _ = _patched(events, prepare=UpdateCheckError("x"))
    with p_prepare, p_params, p_launch:
        _run(controller)  # must not raise: a raise in a task shuts Wheelhouse down
    controller.request_shutdown.assert_not_called()


@pytest.mark.parametrize(
    "error", [UpdateCheckError("the installer does not match the digest"), OSError("disk")]
)
def test_a_failed_download_starts_nothing_and_closes_nothing(error, caplog):
    events = []
    controller = _controller(events)
    p_prepare, p_params, p_launch, fake_launch, _ = _patched(events, prepare=error)
    with p_prepare, p_params, p_launch, caplog.at_level(logging.INFO, logger="main"):
        _run(controller)
    assert events == ["prepare"]
    controller.request_shutdown.assert_not_called()
    lines = _warnings(caplog)
    assert len(lines) == 1
    assert VERSION in lines[0] and str(error) in lines[0]
    assert _gui_messages(controller) == [FAILURE_MESSAGE]


def test_an_installer_that_does_not_start_leaves_wheelhouse_running(caplog):
    events = []
    controller = _controller(events)
    p_prepare, p_params, p_launch, _, _ = _patched(events, launch_result=False)
    with p_prepare, p_params, p_launch, caplog.at_level(logging.INFO, logger="main"):
        _run(controller)
    assert events == ["prepare", "launch"]
    controller.request_shutdown.assert_not_called()
    lines = _warnings(caplog)
    assert len(lines) == 1 and VERSION in lines[0]
    assert _gui_messages(controller) == [FAILURE_MESSAGE]


def test_a_request_during_shutdown_does_nothing():
    events = []
    controller = _controller(events)
    controller.shutdown_event = MagicMock()
    controller.shutdown_event.is_set.return_value = True
    p_prepare, p_params, p_launch, _, _ = _patched(events)
    with p_prepare, p_params, p_launch:
        _run(controller)
    assert events == []


def test_the_download_runs_in_a_worker_thread():
    import threading

    controller = _controller([])
    seen = {}

    def fake_prepare(tag, running_version):
        seen["thread"] = threading.current_thread()
        raise UpdateCheckError("stop here")

    with patch("main.update_install.prepare_update", side_effect=fake_prepare):
        _run(controller)
    assert seen["thread"] is not threading.main_thread()


def test_the_handler_is_started_as_a_task_so_the_event_loop_stays_free():
    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller.state_manager = MagicMock()
    command = {"action": "update_now", "version": VERSION, "tag": TAG}
    handlers = LogicController._build_gui_handler_map(controller, command)
    handlers["update_now"]()
    controller.create_task_with_error_handling.assert_called_once()
    assert controller.create_task_with_error_handling.call_args.args[1] == "UpdateNow"
def test_the_handler_logs_one_line_naming_the_version_and_the_tag(caplog):
    # The log line is the only record of which release the user asked for.
    # A bug report that holds the version but not the tag cannot say which
    # release the user saw.
    events = []
    controller = _controller(events)
    p_prepare, p_params, p_launch, _fake, _prepared = _patched(events)
    with caplog.at_level(logging.INFO, logger="main"), p_prepare, p_params, p_launch:
        _run(controller)
    opening = [
        record.getMessage()
        for record in caplog.records
        if record.name == "main" and record.getMessage().startswith("Update now requested")
    ]
    assert len(opening) == 1
    assert repr(VERSION) in opening[0]
    assert repr(TAG) in opening[0]


# --- the engine of the running Wheelhouse (wh-release-update-check.1.3) ------


def test_the_caller_passes_the_runtime_engine_to_the_parameter_builder():
    events = []
    controller = _controller(events)
    controller.state_manager.get_running_remote_stt_provider.return_value = "google_stt"
    p_prepare, _, p_launch, _, _ = _patched(events)
    with p_prepare, p_launch, patch(
        "main.update_install.build_installer_parameters", return_value=list(PARAMS)
    ) as build:
        _run(controller)
    assert build.call_count == 1
    assert build.call_args.args == (controller.config_service,)
    assert build.call_args.kwargs.get("running_engine") == "google_stt"


def test_a_scalar_stt_config_launches_the_installer_with_the_running_engine(tmp_path):
    from config_service import ConfigService

    path = tmp_path / "config.toml"
    path.write_text('stt = "whisper"\n', encoding="utf-8")
    events = []
    controller = _controller(events)
    controller.config_service = ConfigService(str(path))
    controller.state_manager.get_running_remote_stt_provider.return_value = "parakeet_tdt"
    p_prepare, _, p_launch, fake_launch, _ = _patched(events)
    with p_prepare, p_launch:  # build_installer_parameters runs for real
        _run(controller)
    assert events == ["prepare", "launch", "shutdown"]
    launched = fake_launch.call[2]["installer_parameters"]
    assert "-SttProvider" in launched
    assert launched[launched.index("-SttProvider") + 1] in update_install.VALID_STT_PROVIDERS
    assert launched[launched.index("-SttProvider") + 1] == "parakeet_tdt"
