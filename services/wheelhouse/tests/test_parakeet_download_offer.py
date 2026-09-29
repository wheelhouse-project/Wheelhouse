"""The Parakeet model download offer, menu to installer launch
(wh-parakeet-model-download-offer, stage build-2).

When the user picks Parakeet and its speech model is not on the computer,
Wheelhouse offers to run the installer again instead of starting an engine
that cannot load. The parts covered here:

  * The tray-menu query. discover_providers() leaves a disabled provider
    out, and its callers choose the startup engine and the fallback, so its
    answer must not change (boss ruling B1, condition). A separate query,
    which only the tray-menu builder calls, reports a disabled Parakeet
    whose model is incomplete as "Parakeet (model not installed)".
  * The Logic check in _switch_stt_provider (A1, A2 Not now, A4).
  * Download now: the installer launch, then the ordinary shutdown (A2).
  * The GUI side: the two ruled notices, Copy command, Not now.

stt.parakeet_model itself is covered by test_parakeet_model.py.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

from stt import parakeet_model as pm  # noqa: E402

# The GUI tests build GuiManager and real toast widgets; see the same
# marker in test_gui_grant_prompt.py (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")

MENU_LABEL = "Parakeet (model not installed)"
SCRIPT = Path(r"C:\Users\someone\AppData\Local\WheelhouseSetup\install-wheelhouse.ps1")

OFFER_TITLE = "Parakeet speech model not installed"
LAUNCH_BODY = (
    "Parakeet needs its speech model, which is not on this computer. The "
    "download is about 2.5 GB. Download now closes Wheelhouse and runs the "
    "Wheelhouse installer again. The installer repeats its whole setup for "
    "several minutes, then downloads the model and starts Wheelhouse with "
    "Parakeet."
)
FALLBACK_BODY = (
    "Parakeet needs its speech model, which is not on this computer. The "
    "download is about 2.5 GB. To install it, close Wheelhouse, open "
    "PowerShell, and run the Wheelhouse install command. When the installer "
    "asks which speech engine to use, choose Parakeet. The installer repeats "
    "its whole setup for several minutes."
)


# ---------------------------------------------------------------------------
# Provider trees on disk
# ---------------------------------------------------------------------------


def _write_provider(root: Path, dirname: str, name: str, *, enabled: bool,
                    model_path: Path | None = None) -> Path:
    service_dir = root / dirname
    service_dir.mkdir()
    lines = [
        "[provider]",
        f'name = "{name}"',
        f'display_name = "{name} engine"',
        'launcher = "launcher.py"',
        f"enabled = {'true' if enabled else 'false'}",
    ]
    if model_path is not None:
        # A TOML literal string, so a Windows path needs no escaping.
        lines += ["", "[model]", f"model_path = '{model_path}'"]
    (service_dir / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (service_dir / "launcher.py").write_text("", encoding="utf-8")
    return service_dir


def _complete_the_model(model_dir: Path) -> None:
    for name in ("tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx",
                 "joiner.int8.onnx"):
        (model_dir / name).write_bytes(b"x")


@pytest.fixture
def stt_tree(tmp_path, monkeypatch):
    """Google enabled; Parakeet disabled, pointing at an empty model folder.

    LOCALAPPDATA points into tmp_path, so no override file of the machine
    running the test is read.
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    services = tmp_path / "stt_providers"
    services.mkdir()
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    _write_provider(services, "google_stt_server", "google_stt", enabled=True)
    parakeet_dir = _write_provider(
        services, "sherpa_offline_parakeet_stt_server", "parakeet_tdt",
        enabled=False, model_path=model_dir,
    )
    return {"services": services, "parakeet_dir": parakeet_dir,
            "model_dir": model_dir, "tmp_path": tmp_path}


def _launcher(tree):
    from stt.remote_stt_launcher import RemoteSTTLauncher

    return RemoteSTTLauncher(
        services_dir=tree["services"],
        app_data_dir=tree["tmp_path"] / "appdata",
    )


def _state_manager(launcher):
    import asyncio

    from state_manager import StateManager

    config_service = MagicMock()
    config_service.get.side_effect = lambda key, default=None: {
        "stt.last_provider": "google_stt",
    }.get(key, default)
    loop = asyncio.new_event_loop()
    sm = StateManager(
        config_service=config_service,
        event_bus=MagicMock(),
        loop=loop,
        state_to_gui_queue=Queue(),
        websocket_manager=None,
    )
    sm.set_remote_stt_launcher(launcher)
    return sm, loop


# ---------------------------------------------------------------------------
# The tray-menu query (boss ruling B1)
# ---------------------------------------------------------------------------


class TestTheMenuQuery:
    def test_disabled_parakeet_is_left_out_of_discovery_and_listed_in_the_menu(
        self, stt_tree
    ):
        """The boss's red-first test for B1: discovery unchanged, menu entry added."""
        launcher = _launcher(stt_tree)
        sm, loop = _state_manager(launcher)
        try:
            sm.send_state_update()
            msg = sm.state_to_gui_queue.get_nowait()
        finally:
            loop.close()

        assert [p["name"] for p in launcher.discover_providers()] == ["google_stt"]
        assert msg["stt_providers_available"] == ["google_stt"]
        assert "parakeet_tdt" not in msg["stt_provider_display_names"]
        assert msg["stt_providers_not_installed"] == {"parakeet_tdt": MENU_LABEL}

    def test_the_query_names_the_disabled_parakeet(self, stt_tree):
        launcher = _launcher(stt_tree)
        assert launcher.get_not_installed_providers() == [
            {"name": "parakeet_tdt", "display_name": MENU_LABEL},
        ]

    def test_the_query_leaves_discovery_unchanged(self, stt_tree):
        launcher = _launcher(stt_tree)
        before = launcher.discover_providers()
        launcher.get_not_installed_providers()
        assert launcher.discover_providers() == before
        assert launcher.get_providers() == before

    def test_nothing_is_listed_when_the_model_is_complete(self, stt_tree):
        _complete_the_model(stt_tree["model_dir"])
        launcher = _launcher(stt_tree)
        assert launcher.get_not_installed_providers() == []

    def test_the_model_is_checked_again_at_every_query(self, stt_tree):
        launcher = _launcher(stt_tree)
        assert launcher.get_not_installed_providers() != []
        _complete_the_model(stt_tree["model_dir"])
        assert launcher.get_not_installed_providers() == []

    def test_an_enabled_parakeet_is_listed_by_discovery_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        services = tmp_path / "stt_providers"
        services.mkdir()
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        _write_provider(services, "sherpa_offline_parakeet_stt_server",
                        "parakeet_tdt", enabled=True, model_path=model_dir)
        launcher = _launcher({"services": services, "tmp_path": tmp_path})

        assert [p["name"] for p in launcher.discover_providers()] == ["parakeet_tdt"]
        assert launcher.get_not_installed_providers() == []

    def test_an_enabled_parakeet_that_discovery_leaves_out_is_not_listed(
        self, tmp_path, monkeypatch
    ):
        """Only enabled = false earns the entry, not any Parakeet discovery drops.

        Discovery also drops an enabled provider whose launcher file is
        missing. Such a Parakeet is not the installer's disabled Parakeet, so
        the menu query must not report it, even with its model incomplete.
        """
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        services = tmp_path / "stt_providers"
        services.mkdir()
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        parakeet_dir = _write_provider(
            services, "sherpa_offline_parakeet_stt_server", "parakeet_tdt",
            enabled=True, model_path=model_dir,
        )
        (parakeet_dir / "launcher.py").unlink()
        launcher = _launcher({"services": services, "tmp_path": tmp_path})

        assert launcher.discover_providers() == []
        assert launcher.get_not_installed_providers() == []

    def test_a_disabled_provider_other_than_parakeet_is_not_listed(
        self, stt_tree
    ):
        _write_provider(stt_tree["services"], "distil_medium_en", "distil_medium_en",
                        enabled=False)
        launcher = _launcher(stt_tree)
        names = [p["name"] for p in launcher.get_not_installed_providers()]
        assert names == ["parakeet_tdt"]

    def test_a_disabled_provider_is_not_taken_for_parakeet(self, tmp_path, monkeypatch):
        """With no Parakeet folder at all, another disabled engine lists nothing.

        The test above also holds a disabled Parakeet, so it would still name
        Parakeet if the query took the other engine's folder for it.
        """
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        services = tmp_path / "stt_providers"
        services.mkdir()
        _write_provider(services, "distil_medium_en", "distil_medium_en",
                        enabled=False)
        launcher = _launcher({"services": services, "tmp_path": tmp_path})

        assert launcher.get_not_installed_providers() == []

    def test_a_disabled_parakeet_template_is_not_listed(self, stt_tree):
        config = stt_tree["parakeet_dir"] / "config.toml"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                "enabled = false", "enabled = false\ntemplate = true"),
            encoding="utf-8",
        )
        launcher = _launcher(stt_tree)
        assert launcher.get_not_installed_providers() == []

    def test_a_missing_services_folder_lists_nothing(self, tmp_path):
        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = RemoteSTTLauncher(
            services_dir=tmp_path / "absent", app_data_dir=tmp_path / "appdata",
        )
        assert launcher.get_not_installed_providers() == []

    def test_a_failing_query_leaves_the_menu_entries_empty(self):
        launcher = MagicMock()
        launcher.get_providers.return_value = []
        launcher.get_not_installed_providers.side_effect = OSError("disk")
        sm, loop = _state_manager(launcher)
        try:
            assert sm._get_not_installed_stt_providers() == {}
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Provider process ids for the launch console
# ---------------------------------------------------------------------------


class TestRunningProviderPids:
    def test_live_subprocesses_and_live_pid_files_are_listed(self, stt_tree):
        launcher = _launcher(stt_tree)
        launcher.app_data_dir.mkdir(parents=True, exist_ok=True)
        alive = MagicMock(pid=5)
        alive.poll.return_value = None
        exited = MagicMock(pid=6)
        exited.poll.return_value = 0
        launcher._subprocesses = {"google_stt": alive, "distil_medium_en": exited}
        (launcher.app_data_dir / "google_stt.pid").write_text(str(os.getpid()))

        assert launcher.running_provider_pids() == [5, os.getpid()]

    def test_a_dead_or_unreadable_pid_file_is_skipped(self, stt_tree):
        launcher = _launcher(stt_tree)
        launcher.app_data_dir.mkdir(parents=True, exist_ok=True)
        launcher._providers = [{"name": "google_stt"}, {"name": "parakeet_tdt"}]
        (launcher.app_data_dir / "google_stt.pid").write_text("not a number")
        with patch("stt.remote_stt_launcher.psutil.pid_exists", return_value=False):
            (launcher.app_data_dir / "parakeet_tdt.pid").write_text("424242")
            assert launcher.running_provider_pids() == []


# A PID file can outlive its provider (an unclean exit, a Windows restart),
# and Windows reuses process ids, so a live process named in an old file can
# be an unrelated program. The child launcher writes the file after the
# provider starts, so the real provider is older than its file (A7).

_WRITTEN = 1_000_000.0  # the PID file's modification time in these tests
_FOREIGN_PID = 424242


def _pid_file_launcher(stt_tree):
    launcher = _launcher(stt_tree)
    launcher.app_data_dir.mkdir(parents=True, exist_ok=True)
    launcher._providers = [{"name": "parakeet_tdt"}]
    pid_file = launcher.app_data_dir / "parakeet_tdt.pid"
    pid_file.write_text(str(_FOREIGN_PID))
    os.utime(pid_file, (_WRITTEN, _WRITTEN))
    return launcher


def _process(monkeypatch, *, create_time=None, create_raises=None,
             name="python.exe", name_raises=None, init_raises=None):
    """Stand in psutil.Process for the id in the PID file; it exists."""
    import psutil

    monkeypatch.setattr("stt.remote_stt_launcher.psutil.pid_exists",
                        lambda pid: pid == _FOREIGN_PID)
    process = MagicMock()
    if create_raises is not None:
        process.create_time.side_effect = create_raises
    else:
        process.create_time.return_value = create_time
    if name_raises is not None:
        process.name.side_effect = name_raises
    else:
        process.name.return_value = name
    seen = []

    def make(pid):
        seen.append(pid)
        if init_raises is not None:
            raise init_raises
        return process

    monkeypatch.setattr("stt.remote_stt_launcher.psutil.Process", make)
    return psutil, seen


class TestAPidFileIsOlderThanItsProcess:
    def test_a_process_newer_than_the_pid_file_is_not_counted(
        self, stt_tree, monkeypatch
    ):
        launcher = _pid_file_launcher(stt_tree)
        _psutil, seen = _process(monkeypatch, create_time=_WRITTEN + 3.0)

        assert launcher.running_provider_pids() == []
        assert seen == [_FOREIGN_PID]

    def test_a_process_older_than_the_pid_file_is_counted(
        self, stt_tree, monkeypatch
    ):
        launcher = _pid_file_launcher(stt_tree)
        _process(monkeypatch, create_time=_WRITTEN - 60.0)

        assert launcher.running_provider_pids() == [_FOREIGN_PID]

    def test_a_process_started_just_after_the_file_time_is_counted(
        self, stt_tree, monkeypatch
    ):
        launcher = _pid_file_launcher(stt_tree)
        _process(monkeypatch, create_time=_WRITTEN + 1.5)

        assert launcher.running_provider_pids() == [_FOREIGN_PID]

    @pytest.mark.parametrize(
        "name",
        [pytest.param("python.exe", id="python"),
         pytest.param("Pythonw.EXE", id="pythonw-mixed-case"),
         pytest.param("uv.exe", id="uv")],
    )
    def test_access_denied_counts_a_provider_process_name(
        self, stt_tree, monkeypatch, name
    ):
        import psutil

        launcher = _pid_file_launcher(stt_tree)
        _process(monkeypatch, create_raises=psutil.AccessDenied(_FOREIGN_PID),
                 name=name)

        assert launcher.running_provider_pids() == [_FOREIGN_PID]

    def test_access_denied_does_not_count_another_program(
        self, stt_tree, monkeypatch
    ):
        import psutil

        launcher = _pid_file_launcher(stt_tree)
        _process(monkeypatch, create_raises=psutil.AccessDenied(_FOREIGN_PID),
                 name="notepad.exe")

        assert launcher.running_provider_pids() == []

    def test_access_denied_and_an_unreadable_name_is_not_counted(
        self, stt_tree, monkeypatch
    ):
        import psutil

        launcher = _pid_file_launcher(stt_tree)
        _process(monkeypatch, create_raises=psutil.AccessDenied(_FOREIGN_PID),
                 name_raises=psutil.AccessDenied(_FOREIGN_PID))

        assert launcher.running_provider_pids() == []

    def test_a_process_that_exits_before_the_check_is_skipped(
        self, stt_tree, monkeypatch
    ):
        import psutil

        launcher = _pid_file_launcher(stt_tree)
        _process(monkeypatch, create_raises=psutil.NoSuchProcess(_FOREIGN_PID))

        assert launcher.running_provider_pids() == []

    def test_a_process_gone_at_lookup_is_skipped(self, stt_tree, monkeypatch):
        import psutil

        launcher = _pid_file_launcher(stt_tree)
        _process(monkeypatch, init_raises=psutil.NoSuchProcess(_FOREIGN_PID))

        assert launcher.running_provider_pids() == []

    def test_any_other_failure_is_skipped_and_never_raised(
        self, stt_tree, monkeypatch
    ):
        launcher = _pid_file_launcher(stt_tree)
        _process(monkeypatch, create_raises=RuntimeError("psutil broke"))

        assert launcher.running_provider_pids() == []


# ---------------------------------------------------------------------------
# The Logic check in _switch_stt_provider (A1, A2 Not now, A4)
# ---------------------------------------------------------------------------


def _controller(current="google_stt"):
    """A stand-in ``self`` for LogicController methods.

    The same shape as the controllers in test_ui_provider_switching.py:
    a ``MagicMock(spec=LogicController)`` with a real queue to the GUI.
    """
    from main import LogicController

    config_service = MagicMock()
    config_service.get.side_effect = lambda key, default=None: {
        "stt.last_provider": current,
    }.get(key, default)
    config_service.set = MagicMock()
    config_service.save = AsyncMock(return_value=True)

    remote_launcher = MagicMock()
    remote_launcher.stop_provider = AsyncMock(return_value=True)
    remote_launcher.start_provider = MagicMock(return_value=True)
    remote_launcher.is_running = MagicMock(return_value=False)

    service_manager = MagicMock()
    service_manager.remote_stt_launcher = remote_launcher

    state_manager = MagicMock()
    state_manager.state_to_gui_queue = Queue()
    state_manager._get_current_stt_provider.return_value = current

    controller = MagicMock(spec=LogicController)
    controller.config_service = config_service
    controller.service_manager = service_manager
    controller.state_manager = state_manager
    controller.shutdown_event = MagicMock()
    controller.shutdown_event.is_set.return_value = False
    return controller


def _gui_messages(controller):
    messages = []
    queue = controller.state_manager.state_to_gui_queue
    while True:
        try:
            messages.append(queue.get_nowait())
        except Empty:
            return messages


async def _switch(controller, provider):
    from main import LogicController

    await LogicController._switch_stt_provider(controller, provider)


class TestTheLogicCheck:
    @pytest.mark.asyncio
    async def test_an_incomplete_model_sends_the_launch_offer_and_changes_nothing(self):
        controller = _controller()
        launcher = controller.service_manager.remote_stt_launcher
        with patch.object(pm, "parakeet_model_complete", return_value=False), \
             patch.object(pm, "locate_installer",
                          return_value=pm.InstallerLaunchPlan(script_path=SCRIPT)):
            await _switch(controller, "parakeet_tdt")

        assert _gui_messages(controller) == [
            {"action": "parakeet_model_offer", "kind": "launch"},
        ]
        launcher.stop_provider.assert_not_called()
        launcher.start_provider.assert_not_called()
        controller.config_service.set.assert_not_called()
        controller.config_service.save.assert_not_called()
        controller.state_manager.set_running_remote_stt_provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_setup_copy_sends_the_fallback_offer(self):
        controller = _controller()
        fallback = pm.InstallerFallback(
            command=pm.ONE_LINE_INSTALL_COMMAND, reason="no_setup_copy",
        )
        with patch.object(pm, "parakeet_model_complete", return_value=False), \
             patch.object(pm, "locate_installer", return_value=fallback):
            await _switch(controller, "parakeet_tdt")

        assert _gui_messages(controller) == [{
            "action": "parakeet_model_offer",
            "kind": "fallback",
            "command": pm.ONE_LINE_INSTALL_COMMAND,
        }]
        controller.service_manager.remote_stt_launcher.stop_provider.assert_not_called()
        controller.config_service.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_complete_model_switches_as_today(self):
        controller = _controller()
        launcher = controller.service_manager.remote_stt_launcher
        locate = MagicMock()
        with patch.object(pm, "parakeet_model_complete", return_value=True), \
             patch.object(pm, "locate_installer", locate):
            await _switch(controller, "parakeet_tdt")

        launcher.stop_provider.assert_awaited_once_with("google_stt")
        launcher.start_provider.assert_called_once_with("parakeet_tdt")
        controller.config_service.set.assert_any_call("stt.last_provider", "parakeet_tdt")
        controller.config_service.save.assert_awaited()
        locate.assert_not_called()
        assert [m for m in _gui_messages(controller)
                if m.get("action") == "parakeet_model_offer"] == []

    @pytest.mark.asyncio
    async def test_the_model_is_checked_at_every_pick(self):
        controller = _controller()
        check = MagicMock(return_value=False)
        with patch.object(pm, "parakeet_model_complete", check), \
             patch.object(pm, "locate_installer",
                          return_value=pm.InstallerLaunchPlan(script_path=SCRIPT)):
            await _switch(controller, "parakeet_tdt")
            await _switch(controller, "parakeet_tdt")
        assert check.call_count == 2
        assert len(_gui_messages(controller)) == 2

    @pytest.mark.asyncio
    async def test_another_engine_never_checks_the_parakeet_model(self):
        controller = _controller()
        check = MagicMock(side_effect=AssertionError("checked"))
        with patch.object(pm, "parakeet_model_complete", check):
            await _switch(controller, "distil_medium_en")
        check.assert_not_called()
        controller.service_manager.remote_stt_launcher.start_provider.assert_called_once_with(
            "distil_medium_en")

    @pytest.mark.asyncio
    async def test_the_running_engine_returns_before_the_check(self):
        controller = _controller(current="parakeet_tdt")
        check = MagicMock(side_effect=AssertionError("checked"))
        with patch.object(pm, "parakeet_model_complete", check):
            await _switch(controller, "parakeet_tdt")
        check.assert_not_called()
        assert _gui_messages(controller) == []


# ---------------------------------------------------------------------------
# Download now (A2)
# ---------------------------------------------------------------------------


async def _download_now(controller):
    from main import LogicController

    await LogicController._handle_parakeet_download_now(controller)


class TestDownloadNow:
    @pytest.mark.asyncio
    async def test_the_installer_starts_with_the_pids_then_wheelhouse_shuts_down(self):
        controller = _controller()
        controller._wheelhouse_process_ids = MagicMock(return_value=[11, 22, 33])
        launch = MagicMock(return_value=True)
        with patch.object(pm, "locate_installer",
                          return_value=pm.InstallerLaunchPlan(script_path=SCRIPT)), \
             patch.object(pm, "launch_installer", launch):
            await _download_now(controller)

        launch.assert_called_once_with(SCRIPT, [11, 22, 33])
        controller.request_shutdown.assert_called_once_with()
        assert _gui_messages(controller) == []

    @pytest.mark.asyncio
    async def test_a_failed_start_sends_the_fallback_and_keeps_running(self, caplog):
        controller = _controller()
        controller._wheelhouse_process_ids = MagicMock(return_value=[11])
        with patch.object(pm, "locate_installer",
                          return_value=pm.InstallerLaunchPlan(script_path=SCRIPT)), \
             patch.object(pm, "launch_installer", return_value=False), \
             caplog.at_level(logging.WARNING):
            await _download_now(controller)

        controller.request_shutdown.assert_not_called()
        assert _gui_messages(controller) == [{
            "action": "parakeet_model_offer",
            "kind": "fallback",
            "command": pm.ONE_LINE_INSTALL_COMMAND,
        }]
        assert any(r.levelno == logging.WARNING and "installer" in r.getMessage()
                   for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_locator_fallback_at_the_press_shows_the_fallback(self):
        controller = _controller()
        controller._wheelhouse_process_ids = MagicMock(return_value=[11])
        launch = MagicMock(return_value=True)
        fallback = pm.InstallerFallback(
            command=pm.ONE_LINE_INSTALL_COMMAND, reason="version_mismatch",
        )
        with patch.object(pm, "locate_installer", return_value=fallback), \
             patch.object(pm, "launch_installer", launch):
            await _download_now(controller)

        launch.assert_not_called()
        controller.request_shutdown.assert_not_called()
        assert [m["kind"] for m in _gui_messages(controller)] == ["fallback"]

    def test_the_gui_command_reaches_the_handler(self):
        from main import LogicController

        controller = _controller()
        handlers = LogicController._build_gui_handler_map(
            controller, {"action": "parakeet_download_now"},
        )
        handlers["parakeet_download_now"]()
        controller._handle_parakeet_download_now.assert_called_once_with()
        controller.create_task_with_error_handling.assert_called_once()


class TestWheelhouseProcessIds:
    def test_logic_the_launcher_its_children_and_the_providers(self):
        from main import LogicController

        controller = _controller()
        controller.service_manager.remote_stt_launcher.running_provider_pids.return_value = [
            200, 101,
        ]
        parent = MagicMock(pid=50)
        parent.children.return_value = [MagicMock(pid=100), MagicMock(pid=101),
                                        MagicMock(pid=102)]
        process = MagicMock()
        process.parent.return_value = parent
        with patch("os.getpid", return_value=100), \
             patch("psutil.Process", return_value=process):
            pids = LogicController._wheelhouse_process_ids(controller)

        assert pids == [100, 50, 101, 102, 200]

    def test_a_psutil_failure_still_names_this_process_and_the_providers(self):
        from main import LogicController

        controller = _controller()
        controller.service_manager.remote_stt_launcher.running_provider_pids.return_value = [
            200,
        ]
        with patch("os.getpid", return_value=100), \
             patch("psutil.Process", side_effect=OSError("denied")):
            pids = LogicController._wheelhouse_process_ids(controller)

        assert pids == [100, 200]


# ---------------------------------------------------------------------------
# The GUI side (A1 text, A2 buttons)
# ---------------------------------------------------------------------------


@pytest.fixture
def manager():
    with patch("gui.FloatingButton"), \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        from gui import GuiManager
        mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
        # A MagicMock event reports "set", and the queue check then
        # shuts down before it reads the queue.
        mgr.shutdown_event.is_set.return_value = False
        yield mgr
        toast = getattr(mgr, "_parakeet_offer_toast", None)
        if toast is not None and hasattr(toast, "deleteLater"):
            toast.close()
            toast.deleteLater()


LAUNCH_OFFER = {"action": "parakeet_model_offer", "kind": "launch"}
FALLBACK_OFFER = {
    "action": "parakeet_model_offer",
    "kind": "fallback",
    "command": pm.ONE_LINE_INSTALL_COMMAND,
}


class TestTheGuiOffer:
    def test_the_launch_offer_shows_the_ruled_text(self, manager):
        with patch("grant_prompt_toast.GrantPromptToast") as toast_cls:
            manager._show_parakeet_model_offer(LAUNCH_OFFER)
        toast_cls.return_value.show_prompt.assert_called_once_with(
            title=OFFER_TITLE,
            body=LAUNCH_BODY,
            yes_label="Download now",
            no_label="Not now",
            lifetime_ms=None,
        )

    def test_the_fallback_offer_shows_the_ruled_text(self, manager):
        with patch("grant_prompt_toast.GrantPromptToast") as toast_cls:
            manager._show_parakeet_model_offer(FALLBACK_OFFER)
        toast_cls.return_value.show_prompt.assert_called_once_with(
            title=OFFER_TITLE,
            body=FALLBACK_BODY,
            yes_label="Copy command",
            no_label="Not now",
            lifetime_ms=None,
        )

    def test_download_now_sends_the_action_and_copies_nothing(self, manager):
        manager._show_parakeet_model_offer(LAUNCH_OFFER)
        with patch("pyperclip.copy") as copy:
            manager._parakeet_offer_toast._yes_button.click()
        manager.commands_to_logic_queue.put_nowait.assert_called_once_with(
            {"action": "parakeet_download_now"}
        )
        copy.assert_not_called()

    def test_copy_command_writes_the_clipboard_only_when_pressed(self, manager):
        with patch("pyperclip.copy") as copy:
            manager._show_parakeet_model_offer(FALLBACK_OFFER)
            copy.assert_not_called()
            manager._parakeet_offer_toast._yes_button.click()
        copy.assert_called_once_with(pm.ONE_LINE_INSTALL_COMMAND)
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_copy_command_uses_the_installer_command_when_none_is_sent(self, manager):
        with patch("pyperclip.copy") as copy:
            manager._show_parakeet_model_offer(
                {"action": "parakeet_model_offer", "kind": "fallback"})
            manager._parakeet_offer_toast._yes_button.click()
        copy.assert_called_once_with(pm.ONE_LINE_INSTALL_COMMAND)

    @pytest.mark.parametrize("offer", [LAUNCH_OFFER, FALLBACK_OFFER])
    def test_not_now_does_nothing(self, manager, offer):
        manager._show_parakeet_model_offer(offer)
        with patch("pyperclip.copy") as copy:
            manager._parakeet_offer_toast._no_button.click()
        assert not manager._parakeet_offer_toast.isVisible()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()
        copy.assert_not_called()

    @pytest.mark.parametrize("offer", [LAUNCH_OFFER, FALLBACK_OFFER])
    def test_closing_the_offer_does_nothing(self, manager, offer):
        manager._show_parakeet_model_offer(offer)
        with patch("pyperclip.copy") as copy:
            manager._parakeet_offer_toast._dismiss_button.click()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()
        copy.assert_not_called()

    def test_the_offer_stays_until_answered(self, manager):
        manager._show_parakeet_model_offer(LAUNCH_OFFER)
        assert manager._parakeet_offer_toast.isVisible()
        assert not manager._parakeet_offer_toast._lifetime_timer.isActive()

    def test_a_second_offer_reuses_the_one_toast(self, manager):
        with patch("grant_prompt_toast.GrantPromptToast") as toast_cls:
            manager._show_parakeet_model_offer(LAUNCH_OFFER)
            manager._show_parakeet_model_offer(LAUNCH_OFFER)
        toast_cls.assert_called_once()
        assert toast_cls.return_value.show_prompt.call_count == 2

    def test_a_fallback_after_a_launch_offer_changes_the_button(self, manager):
        manager._show_parakeet_model_offer(LAUNCH_OFFER)
        manager._show_parakeet_model_offer(FALLBACK_OFFER)
        toast = manager._parakeet_offer_toast
        assert toast._yes_button.text() == "Copy command"
        with patch("pyperclip.copy") as copy:
            toast._yes_button.click()
        copy.assert_called_once_with(pm.ONE_LINE_INSTALL_COMMAND)
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_an_unknown_kind_shows_nothing(self, manager, caplog):
        with patch("grant_prompt_toast.GrantPromptToast") as toast_cls, \
             caplog.at_level(logging.WARNING):
            manager._show_parakeet_model_offer(
                {"action": "parakeet_model_offer", "kind": "other"})
        toast_cls.assert_not_called()
        assert any("parakeet_model_offer" in r.getMessage() for r in caplog.records)

    def test_the_offer_action_reaches_the_handler(self, manager):
        manager.state_from_logic_queue.get_nowait.side_effect = [LAUNCH_OFFER, Empty()]
        with patch.object(manager, "_show_parakeet_model_offer") as show:
            manager._check_queues_and_events()
        show.assert_called_once_with(LAUNCH_OFFER)
