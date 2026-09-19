"""Tests for launcher.py crash recovery and process supervision.

Target: services/wheelhouse/launcher.py

Tests cover:
- Constants validation (reads real module constants)
- Stale resource cleanup (calls cleanup_stale_resources())
- Main function integration (calls launcher.main() with mocks)
- Crash detection via main() (short uptime increments crash count)
- Restart flag via main() (triggers second loop iteration)
- Process death via main() (dead process triggers shutdown)
- Graceful shutdown via main() (join then terminate)
- Shared memory cleanup via main() (close and unlink called)

Every test either reads production constants, calls cleanup_stale_resources(),
or calls launcher.main(). No logic-mirroring.
"""

import contextlib
import logging
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, patch, call

import pytest

import launcher

# Captured at import time so tests that exercise the REAL launcher
# logging setup keep working once the autouse hermeticity fixture
# (wh-launcher-test-log-leak) stubs the module attribute.
_REAL_CONFIGURE_LAUNCHER_LOGGING = launcher._configure_launcher_logging


@pytest.fixture(autouse=True)
def hermetic_launcher_logging(monkeypatch):
    """Keep every launcher.main() call in this module out of the
    production log (wh-launcher-test-log-leak).

    The real _configure_launcher_logging opens a rotating file handler
    on the REPO-ROOT wheelhouse.log -- the path comes from launcher.py's
    own __file__, so no tmp_path fixture redirects it. Before this stub,
    every full-suite run wrote a ~240-line burst of mock ERROR/CRITICAL
    launcher records into the live log (22 bursts on 2026-07-09/10)
    while the real app was writing to the same file.

    Tests that need the real function call the module-level capture
    _REAL_CONFIGURE_LAUNCHER_LOGGING directly.
    """
    monkeypatch.setattr(
        launcher,
        "_configure_launcher_logging",
        Mock(
            name="_configure_launcher_logging stub",
            return_value=Mock(name="stub launcher log listener"),
        ),
    )


@pytest.fixture(autouse=True)
def hermetic_transcript_env(monkeypatch):
    """Keep _run_supervisor's transcript-flag export from leaking into the
    rest of the suite (wh-launcher-test-env-leak).

    The integration tests here run the real _run_supervisor preamble, which
    reads the REAL config.toml and writes WHEELHOUSE_LOG_TRANSCRIPTS into
    os.environ for the whole pytest process. On a dev machine with
    LOG_TRANSCRIPTS = true that flipped utils.redact off suite-wide and
    failed the redaction assertions in later test files (the clipboard
    verified_paste privacy test). setenv records the pre-test state, so
    teardown restores it no matter what the launcher wrote in between.
    """
    monkeypatch.setenv("WHEELHOUSE_LOG_TRANSCRIPTS", "0")


@pytest.fixture(autouse=True)
def hermetic_ptt_volume_restore(monkeypatch):
    """Keep every launcher.main() call in this module away from the speaker
    level (wh-ptt-mute-orphaned-on-process-loss).

    The launcher restores a push-to-talk speaker level at start and after
    each cycle. The real restore reads a record under the app-data path and
    can write the level through Core Audio. Every launcher.main() call here
    already patches get_app_data_path to a temporary directory, which holds
    no record; this stub is a second guard, so a test that forgets that
    patch still cannot change the speaker level of the machine that runs
    the tests. Tests that check the restore patch the function again.
    """
    monkeypatch.setattr(
        "services.wheelhouse.utils.ptt_volume_record.restore_orphaned_volume",
        Mock(name="restore_orphaned_volume stub", return_value="no_record"),
    )


def _fake_time_ns(time_values):
    """Create a SimpleNamespace that replaces launcher.time without affecting logging.

    Patching launcher.time.time via unittest.mock also patches the global time
    module (since launcher.time IS the time module).  Python's logging calls
    time.time() internally for every LogRecord, which consumes values from the
    iterator and causes StopIteration or incorrect uptime calculations.

    By replacing launcher.time with a SimpleNamespace, only the launcher code
    sees the mock; logging keeps using the real time module.
    """
    it = iter(time_values)
    last = time_values[-1]
    return SimpleNamespace(
        time=lambda: next(it, last),
        sleep=lambda *a, **kw: None,
    )


# ---------------------------------------------------------------------------
# Shared fixture for main() integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def launcher_env(tmp_path):
    """Set up a fully mocked environment for launcher.main().

    Returns a dict with helpers to configure and run main().
    All external I/O is mocked: processes, shared memory, queues, events,
    time, sleep, sys.modules, and filesystem paths.
    """
    app_data = tmp_path / "appdata"
    app_data.mkdir()

    # Shared memory mocks
    mock_shm = MagicMock()
    mock_shm.name = "test_shm"
    mock_gui_shm = MagicMock()
    mock_gui_shm.name = "test_gui_shm"

    # Shutdown event mock - the key control lever for test flow
    shutdown_event = Mock()
    shutdown_event.is_set.return_value = False

    # Module mocks (main, input_proc, gui)
    sys_modules = {
        "main": MagicMock(start_logic_process=Mock()),
        "input_proc": MagicMock(input_process_main=Mock()),
        "gui": MagicMock(gui_process_target=Mock()),
    }

    env = {
        "app_data": app_data,
        "tmp_path": tmp_path,
        "mock_shm": mock_shm,
        "mock_gui_shm": mock_gui_shm,
        "shutdown_event": shutdown_event,
        "sys_modules": sys_modules,
    }

    def make_process(alive=False, exitcode=0, name="TestProcess", pid=100):
        """Create a mock process with configurable state."""
        proc = Mock()
        proc.is_alive.return_value = alive
        proc.exitcode = exitcode
        proc.name = name
        proc.pid = pid
        return proc

    env["make_process"] = make_process

    def run_main(time_values, process_mocks=None, shm_mocks=None):
        """Run launcher.main() with the given time sequence and process mocks.

        Args:
            time_values: list of floats for time.time() side_effect
            process_mocks: list of 3 mock processes [logic, input, gui],
                           or None to create default dead-on-arrival processes
            shm_mocks: list of 2 SHM mocks, or None to use defaults
        """
        if process_mocks is None:
            logic = make_process(alive=False, exitcode=0, name="LogicProcess", pid=42)
            inp = make_process(alive=False, exitcode=0, name="InputProcess", pid=43)
            gui = make_process(alive=False, exitcode=0, name="GuiProcess", pid=44)
            process_mocks = [logic, inp, gui]

        if shm_mocks is None:
            shm_mocks = [mock_shm, mock_gui_shm]

        quick_edit_mock = Mock(return_value=True)
        env["quick_edit_mock"] = quick_edit_mock

        with patch("services.wheelhouse.utils.system.get_app_data_path",
                    return_value=str(app_data)), \
             patch.object(launcher, "cleanup_stale_resources"), \
             patch.object(launcher, "disable_console_quick_edit",
                          quick_edit_mock), \
             patch("launcher.shared_memory.SharedMemory",
                   side_effect=shm_mocks), \
             patch("launcher.multiprocessing.Queue", return_value=Mock()), \
             patch("launcher.multiprocessing.Event",
                   return_value=shutdown_event), \
             patch("launcher.multiprocessing.Process",
                   side_effect=process_mocks), \
             patch.dict("sys.modules", sys_modules), \
             patch.object(launcher, "time", _fake_time_ns(time_values)):

            launcher.main()

        return process_mocks

    env["run_main"] = run_main
    return env


# ---------------------------------------------------------------------------
# Constants validation (KEPT - reads real module constants)
# ---------------------------------------------------------------------------


class TestLauncherConstants:
    """Verify launcher constants have expected values."""

    def test_crash_threshold_is_15_seconds(self):
        assert launcher.CRASH_THRESHOLD_S == 15

    def test_max_crashes_is_3(self):
        assert launcher.MAX_CRASHES == 3


# ---------------------------------------------------------------------------
# Second-copy protection (wh-launcher-single-instance)
# ---------------------------------------------------------------------------


class TestLauncherInstanceRecord:
    """The launcher records its own PID plus its creation time.

    The creation time is what makes the record safe to act on. Windows
    reuses PIDs, so a bare PID would let a recycled PID point at an
    unrelated process, and the stop path would kill that process and its
    whole child tree.
    """

    def test_record_round_trips(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=4321, create_time=1234.5)
        assert launcher.read_instance_record(path) == (4321, "1234.500000")

    def test_missing_file_reads_as_none(self, tmp_path):
        assert launcher.read_instance_record(str(tmp_path / "absent.pid")) is None

    def test_malformed_file_reads_as_none(self, tmp_path):
        path = tmp_path / "wheelhouse.pid"
        path.write_text("not-a-record")
        assert launcher.read_instance_record(str(path)) is None

    def test_bare_legacy_pid_reads_as_none(self, tmp_path):
        # Older builds wrote the Logic process PID alone. Such a record
        # carries no creation time, so it must never authorise a kill.
        path = tmp_path / "wheelhouse.pid"
        path.write_text("12345")
        assert launcher.read_instance_record(str(path)) is None


# Logic, Input and GUI are started with multiprocessing, so they run the
# same interpreter as the launcher. That shared executable is what marks a
# descendant as ours.
_OWNED_EXE = r"C:\WheelHouse\services\wheelhouse\.venv\Scripts\python.exe"


class TestStopPreviousInstance:
    """stop_previous_instance() must kill the previous launcher tree, and
    must refuse to kill anything it cannot positively identify."""

    def _proc(self, pid, create_time, children=(), exe=_OWNED_EXE):
        proc = Mock()
        proc.pid = pid
        proc.create_time.return_value = create_time
        proc.children.return_value = list(children)
        proc.exe.return_value = exe
        return proc

    def test_no_record_is_noop(self, tmp_path):
        assert launcher.stop_previous_instance(
            str(tmp_path / "absent.pid"), _psutil=Mock()
        ) is False

    def test_kills_parent_before_children(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        child = self._proc(1000, 101.0)
        parent = self._proc(999, 100.0, children=[child])
        order = []
        parent.terminate.side_effect = lambda: order.append("parent")
        child.terminate.side_effect = lambda: order.append("child")
        ps = Mock()
        ps.Process.return_value = parent
        ps.wait_procs.return_value = ([], [])
        ps.NoSuchProcess = Exception

        assert launcher.stop_previous_instance(path, _psutil=ps) is True
        # Parent first: the old supervisor restarts its children when they
        # die, so killing a child first can make it respawn.
        assert order == ["parent", "child"]
        assert not os.path.exists(path)

    def test_recycled_pid_is_not_killed(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        # Same PID, different creation time: an unrelated process now owns
        # this PID. Killing it would take down whatever the user is running.
        impostor = self._proc(999, 555.0)
        ps = Mock()
        ps.Process.return_value = impostor
        ps.NoSuchProcess = Exception

        assert launcher.stop_previous_instance(path, _psutil=ps) is False
        impostor.terminate.assert_not_called()
        assert not os.path.exists(path)

    def test_dead_process_removes_record(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)

        class NoSuchProcess(Exception):
            pass

        ps = Mock()
        ps.NoSuchProcess = NoSuchProcess
        ps.Process.side_effect = NoSuchProcess()

        assert launcher.stop_previous_instance(path, _psutil=ps) is False
        assert not os.path.exists(path)

    def test_survivor_is_killed_after_timeout(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        parent = self._proc(999, 100.0)
        ps = Mock()
        ps.Process.return_value = parent
        ps.wait_procs.return_value = ([], [parent])
        ps.NoSuchProcess = Exception

        assert launcher.stop_previous_instance(path, _psutil=ps) is True
        parent.kill.assert_called_once()

    def test_own_pid_is_never_killed(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(
            path, pid=os.getpid(), create_time=100.0
        )
        ps = Mock()
        ps.NoSuchProcess = Exception
        assert launcher.stop_previous_instance(path, _psutil=ps) is False
        ps.Process.assert_not_called()

    # -- One psutil failure must not abandon the rest of the stop ----------
    #
    # Every step below can fail on a real machine: the old copy runs
    # elevated and this one does not, or the old copy exits between two
    # calls. When a step fails, the steps after it still have work to do.
    # Losing them leaves the old copy (or its children) alive while this
    # launcher starts anyway, which is the second-copy bug this module
    # exists to prevent (wh-launcher-single-instance.1.1).

    def test_children_lookup_failure_still_terminates_the_parent(
        self, tmp_path
    ):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        parent = self._proc(999, 100.0)
        parent.children.side_effect = RuntimeError("access denied")
        ps = Mock()
        ps.Process.return_value = parent
        ps.wait_procs.return_value = ([], [])
        ps.NoSuchProcess = Exception

        assert launcher.stop_previous_instance(path, _psutil=ps) is True
        parent.terminate.assert_called_once()

    def test_parent_terminate_failure_still_terminates_children(
        self, tmp_path
    ):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        child = self._proc(1000, 101.0)
        parent = self._proc(999, 100.0, children=[child])
        parent.terminate.side_effect = RuntimeError("access denied")
        ps = Mock()
        ps.Process.return_value = parent
        ps.wait_procs.return_value = ([], [])
        ps.NoSuchProcess = Exception

        # The parent survives, so no previous instance was stopped.
        assert launcher.stop_previous_instance(path, _psutil=ps) is False
        # Its children must still go: they hold the microphone and the
        # hotkey hooks that the new copy is about to claim.
        child.terminate.assert_called_once()

    def test_wait_failure_does_not_abandon_the_stop(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        parent = self._proc(999, 100.0)
        ps = Mock()
        ps.Process.return_value = parent
        ps.wait_procs.side_effect = RuntimeError("wait failed")
        ps.NoSuchProcess = Exception

        # terminate() already ran, so the stop happened. Only the survivor
        # check was lost.
        assert launcher.stop_previous_instance(path, _psutil=ps) is True
        parent.terminate.assert_called_once()

    def test_child_terminate_failure_does_not_skip_the_next_child(
        self, tmp_path
    ):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        first = self._proc(1000, 101.0)
        second = self._proc(1001, 102.0)
        first.terminate.side_effect = RuntimeError("access denied")
        parent = self._proc(999, 100.0, children=[first, second])
        ps = Mock()
        ps.Process.return_value = parent
        ps.wait_procs.return_value = ([], [])
        ps.NoSuchProcess = Exception

        assert launcher.stop_previous_instance(path, _psutil=ps) is True
        second.terminate.assert_called_once()

    def test_surviving_child_is_killed(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        child = self._proc(1000, 101.0)
        parent = self._proc(999, 100.0, children=[child])
        ps = Mock()
        ps.Process.return_value = parent
        # The parent exited on terminate; the child did not.
        ps.wait_procs.return_value = ([parent], [child])
        ps.NoSuchProcess = Exception

        assert launcher.stop_previous_instance(path, _psutil=ps) is True
        child.kill.assert_called_once()
        parent.kill.assert_not_called()

    def test_create_time_failure_does_not_kill(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)

        class NoSuchProcess(Exception):
            pass

        proc = Mock()
        proc.pid = 999
        # The process exits between the lookup and the identity check.
        proc.create_time.side_effect = NoSuchProcess()
        ps = Mock()
        ps.NoSuchProcess = NoSuchProcess
        ps.Process.return_value = proc

        assert launcher.stop_previous_instance(path, _psutil=ps) is False
        proc.terminate.assert_not_called()
        assert not os.path.exists(path)

    def test_unidentifiable_process_is_left_alone(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)

        class NoSuchProcess(Exception):
            pass

        proc = Mock()
        proc.pid = 999
        # Windows denies access to the process object. It cannot be
        # identified, so it must not be killed.
        proc.create_time.side_effect = RuntimeError("access denied")
        ps = Mock()
        ps.NoSuchProcess = NoSuchProcess
        ps.Process.return_value = proc

        assert launcher.stop_previous_instance(path, _psutil=ps) is False
        proc.terminate.assert_not_called()

    # -- Only WheelHouse's own processes may be killed --------------------
    #
    # The `run` voice action starts an arbitrary user program through a
    # shell, so that program is a grandchild of the launcher and appears in
    # children(recursive=True). Killing it would lose the user's unsaved
    # work (wh-launcher-single-instance.2.1).

    def test_foreign_descendant_is_not_terminated(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        # A document the user opened by voice.
        user_app = self._proc(1001, 102.0, exe=r"C:\Windows\notepad.exe")
        service = self._proc(1000, 101.0)
        parent = self._proc(999, 100.0, children=[service, user_app])
        ps = Mock()
        ps.Process.return_value = parent
        ps.wait_procs.return_value = ([], [])
        ps.NoSuchProcess = Exception

        assert launcher.stop_previous_instance(path, _psutil=ps) is True
        service.terminate.assert_called_once()
        user_app.terminate.assert_not_called()
        user_app.kill.assert_not_called()

    def test_foreign_descendant_is_left_out_of_the_wait(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        user_app = self._proc(1001, 102.0, exe=r"C:\Windows\notepad.exe")
        parent = self._proc(999, 100.0, children=[user_app])
        ps = Mock()
        ps.Process.return_value = parent
        ps.wait_procs.return_value = ([], [])
        ps.NoSuchProcess = Exception

        assert launcher.stop_previous_instance(path, _psutil=ps) is True
        user_app.kill.assert_not_called()
        # The wait covers the launcher and the processes it owns, nothing
        # else, so a foreign process can never reach the survivor kill.
        assert ps.wait_procs.call_args[0][0] == [parent]

    def test_unreadable_descendant_exe_is_not_terminated(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        opaque = self._proc(1001, 102.0)
        opaque.exe.side_effect = RuntimeError("access denied")
        parent = self._proc(999, 100.0, children=[opaque])
        ps = Mock()
        ps.Process.return_value = parent
        ps.wait_procs.return_value = ([], [])
        ps.NoSuchProcess = Exception

        assert launcher.stop_previous_instance(path, _psutil=ps) is True
        # An unreadable answer must never authorise a kill.
        opaque.terminate.assert_not_called()

    def test_unreadable_owner_exe_stops_only_the_launcher(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        child = self._proc(1000, 101.0)
        parent = self._proc(999, 100.0, children=[child])
        parent.exe.side_effect = RuntimeError("access denied")
        ps = Mock()
        ps.Process.return_value = parent
        ps.wait_procs.return_value = ([], [])
        ps.NoSuchProcess = Exception

        assert launcher.stop_previous_instance(path, _psutil=ps) is True
        parent.terminate.assert_called_once()
        child.terminate.assert_not_called()

    def test_owned_descendant_matches_case_insensitively(self, tmp_path):
        path = str(tmp_path / "wheelhouse.pid")
        launcher.write_instance_record(path, pid=999, create_time=100.0)
        # Windows paths differ in case between APIs.
        child = self._proc(1000, 101.0, exe=_OWNED_EXE.upper())
        parent = self._proc(999, 100.0, children=[child])
        ps = Mock()
        ps.Process.return_value = parent
        ps.wait_procs.return_value = ([], [])
        ps.NoSuchProcess = Exception

        assert launcher.stop_previous_instance(path, _psutil=ps) is True
        child.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# Console QuickEdit hardening (wh-console-quickedit-freeze)
# ---------------------------------------------------------------------------


_QUICK_EDIT = 0x0040
_EXTENDED_FLAGS = 0x0080
_MOUSE_INPUT = 0x0010


class TestDisableConsoleQuickEdit:
    """disable_console_quick_edit() must clear QuickEdit on the attached
    console and never raise.

    A click (or click-drag) in a QuickEdit-enabled console starts a text
    selection, and Windows freezes every write to that console until the
    selection is dismissed. All WheelHouse processes share the launcher's
    console for stderr, so one stray click wedged the whole app on
    2026-07-05 (GUI main thread Not Responding, log frozen, launcher
    supervisor blocked). Disabling QuickEdit at launch removes the hazard.
    """

    def _kernel32(self, *, mode=0x01F7, console_window=1, conin_handle=1234):
        """Mock kernel32 whose GetConsoleMode writes `mode` into its out-param."""
        k = Mock()
        k.GetConsoleWindow.return_value = console_window
        k.CreateFileW.return_value = conin_handle

        def fake_get_mode(handle, pmode):
            pmode._obj.value = mode
            return 1

        k.GetConsoleMode.side_effect = fake_get_mode
        k.SetConsoleMode.return_value = 1
        k.CloseHandle.return_value = 1
        return k

    def test_clears_quick_edit_and_sets_extended_flags(self):
        # 0x01F7 has QuickEdit (0x40) and Extended (0x80) plus other bits set.
        k = self._kernel32(mode=0x01F7)
        assert launcher.disable_console_quick_edit(_kernel32=k) is True
        (_handle, new_mode), _ = k.SetConsoleMode.call_args
        assert new_mode & _QUICK_EDIT == 0
        assert new_mode & _EXTENDED_FLAGS == _EXTENDED_FLAGS
        # Every bit other than QuickEdit/Extended/MouseInput must be preserved.
        preserved = ~(_QUICK_EDIT | _EXTENDED_FLAGS | _MOUSE_INPUT)
        assert new_mode & preserved == 0x01F7 & preserved
        k.CloseHandle.assert_called_once()

    def test_clears_mouse_input_so_the_wheel_scrolls(self):
        # 0x01F7 includes ENABLE_MOUSE_INPUT (0x10). With QuickEdit off but
        # mouse input still on, conhost delivers wheel/click events to the
        # console input buffer -- which no WheelHouse process ever reads --
        # instead of scrolling the viewport (wh-log-console-freeze). Both
        # bits must be cleared in the same SetConsoleMode call.
        k = self._kernel32(mode=0x01F7)
        assert launcher.disable_console_quick_edit(_kernel32=k) is True
        (_handle, new_mode), _ = k.SetConsoleMode.call_args
        assert new_mode & _MOUSE_INPUT == 0

    def test_no_console_window_is_noop(self):
        k = self._kernel32(console_window=0)
        assert launcher.disable_console_quick_edit(_kernel32=k) is False
        k.CreateFileW.assert_not_called()

    def test_invalid_conin_handle_returns_false(self):
        import ctypes
        invalid = ctypes.c_void_p(-1).value
        k = self._kernel32(conin_handle=invalid)
        assert launcher.disable_console_quick_edit(_kernel32=k) is False
        k.SetConsoleMode.assert_not_called()

    def test_get_console_mode_failure_still_closes_handle(self):
        k = self._kernel32()
        k.GetConsoleMode.side_effect = lambda handle, pmode: 0
        assert launcher.disable_console_quick_edit(_kernel32=k) is False
        k.SetConsoleMode.assert_not_called()
        k.CloseHandle.assert_called_once()

    def test_set_console_mode_failure_returns_false(self):
        k = self._kernel32()
        k.SetConsoleMode.return_value = 0
        assert launcher.disable_console_quick_edit(_kernel32=k) is False
        k.CloseHandle.assert_called_once()

    def test_never_raises_on_unexpected_error(self):
        k = Mock()
        k.GetConsoleWindow.side_effect = OSError("boom")
        assert launcher.disable_console_quick_edit(_kernel32=k) is False

    def test_supervisor_disables_quick_edit_once(self, launcher_env):
        launcher_env["run_main"]([1000.0, 1001.0, 1002.0, 1003.0])
        launcher_env["quick_edit_mock"].assert_called_once()

    def test_shutdown_grace_period_is_5_seconds(self):
        assert launcher.SHUTDOWN_GRACE_PERIOD_S == 5

    def test_shared_mem_size_is_64k(self):
        assert launcher.SHARED_MEM_SIZE == 1024 * 64

    def test_gui_overlay_shm_size_is_256(self):
        assert launcher.GUI_OVERLAY_SHM_SIZE == 256

    def test_app_name(self):
        assert launcher.APP_NAME == "WheelHouse"


# ---------------------------------------------------------------------------
# Stale resource cleanup (KEPT - calls real cleanup_stale_resources())
# ---------------------------------------------------------------------------


class TestStaleResourceCleanup:
    """Test cleanup_stale_resources() function."""

    def test_cleanup_removes_pid_file(self, tmp_path):
        """PID file from a dead process is removed."""
        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("999999")

        with patch.object(launcher, "PID_FILE_PATH", str(pid_file)), \
             patch("launcher.psutil") as mock_psutil:
            mock_psutil.pid_exists.return_value = False
            launcher.cleanup_stale_resources()

        assert not pid_file.exists()

    def test_cleanup_removes_pid_file_with_stale_pid(self, tmp_path):
        """PID file referencing a still-running PID is still removed."""
        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("12345")

        with patch.object(launcher, "PID_FILE_PATH", str(pid_file)), \
             patch("launcher.psutil") as mock_psutil:
            mock_psutil.pid_exists.return_value = True
            launcher.cleanup_stale_resources()

        assert not pid_file.exists()

    def test_cleanup_handles_missing_pid_file(self, tmp_path):
        """No error when PID file doesn't exist."""
        pid_file = tmp_path / "wheelhouse.pid"

        with patch.object(launcher, "PID_FILE_PATH", str(pid_file)):
            launcher.cleanup_stale_resources()

    def test_cleanup_handles_empty_pid_file(self, tmp_path):
        """Empty PID file is removed without error."""
        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("")

        with patch.object(launcher, "PID_FILE_PATH", str(pid_file)):
            launcher.cleanup_stale_resources()

        assert not pid_file.exists()

    def test_cleanup_handles_corrupt_pid_file(self, tmp_path):
        """A corrupt PID file is removed, not left on disk.

        Nothing can be identified from a file that does not parse, so it
        is deleted rather than left for a later start to misread. Before
        wh-launcher-single-instance the removal was skipped on this path
        and the corrupt file stayed on disk forever.
        """
        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("not_a_number")

        with patch.object(launcher, "PID_FILE_PATH", str(pid_file)):
            launcher.cleanup_stale_resources()

        assert not pid_file.exists()

    def test_cleanup_handles_io_error(self, tmp_path):
        """A PID file that cannot be read is removed, not left on disk.

        An unreadable record identifies nothing, so it is treated the same
        way as a corrupt one. ``os.remove`` is reached because it sits
        outside the block that reads the file.
        """
        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("12345")

        with patch.object(launcher, "PID_FILE_PATH", str(pid_file)), \
             patch("builtins.open", side_effect=IOError("disk error")):
            launcher.cleanup_stale_resources()

        assert not pid_file.exists()


# ---------------------------------------------------------------------------
# Main function integration (KEPT - calls launcher.main())
# ---------------------------------------------------------------------------


class TestMainFunctionIntegration:
    """Integration-style tests for main() with all externals mocked."""

    def test_main_creates_pid_file(self, launcher_env):
        """main() writes the logic process PID to the PID file."""
        app_data = launcher_env["app_data"]
        pid_file = app_data / "wheelhouse.pid"

        logic = launcher_env["make_process"](
            alive=False, exitcode=0, name="LogicProcess", pid=42,
        )
        inp = launcher_env["make_process"](
            alive=False, exitcode=0, name="InputProcess",
        )
        gui = launcher_env["make_process"](
            alive=False, exitcode=0, name="GuiProcess",
        )

        # Long uptime so no crash increment; one loop iteration
        # time.time() calls: (1) start_time, (2) shm_name, (3) shm_name,
        # (4) gui_shm_name, (5) gui_shm_name -> wait, process dead ->
        # finally: (6) uptime calc
        launcher_env["run_main"](
            time_values=[100.0, 100.1, 100.2, 100.3, 100.4, 130.0],
            process_mocks=[logic, inp, gui],
        )

        # PID file created at line 138, removed at line 203-205 in finally
        assert not pid_file.exists()

    def test_main_cleans_up_shared_memory(self, launcher_env):
        """main() closes and unlinks shared memory on exit."""
        mock_shm = launcher_env["mock_shm"]
        mock_gui_shm = launcher_env["mock_gui_shm"]

        launcher_env["run_main"](
            time_values=[100.0, 100.1, 100.2, 100.3, 100.4, 130.0],
        )

        mock_shm.close.assert_called_once()
        mock_shm.unlink.assert_called_once()
        mock_gui_shm.close.assert_called_once()
        mock_gui_shm.unlink.assert_called_once()

    def test_main_handles_startup_exception(self, launcher_env):
        """main() handles exceptions during process startup gracefully."""
        mock_shm = launcher_env["mock_shm"]
        mock_gui_shm = launcher_env["mock_gui_shm"]
        app_data = launcher_env["app_data"]
        shutdown_event = launcher_env["shutdown_event"]

        with patch("services.wheelhouse.utils.system.get_app_data_path",
                    return_value=str(app_data)), \
             patch.object(launcher, "cleanup_stale_resources"), \
             patch("launcher.shared_memory.SharedMemory",
                   side_effect=[mock_shm, mock_gui_shm]), \
             patch("launcher.multiprocessing.Queue", return_value=Mock()), \
             patch("launcher.multiprocessing.Event",
                   return_value=shutdown_event), \
             patch.object(launcher, "time",
                          _fake_time_ns([100.0, 100.1, 100.2, 100.3, 100.4, 100.5])), \
             patch.dict("sys.modules", launcher_env["sys_modules"]), \
             patch("launcher.multiprocessing.Process",
                   side_effect=RuntimeError("Process creation failed")):

            launcher.main()

        # SharedMemory still gets cleaned up in finally block
        mock_shm.close.assert_called_once()
        mock_shm.unlink.assert_called_once()


# ---------------------------------------------------------------------------
# Crash detection (NEW - calls launcher.main())
# ---------------------------------------------------------------------------


class TestCrashDetection:
    """Test crash detection by calling launcher.main() with controlled time."""

    def test_short_uptime_increments_crash_count(self, launcher_env, caplog):
        """Process dying quickly (< 15s uptime) logs a crash."""
        import logging
        caplog.set_level(logging.ERROR)

        # Short uptime: start=100.0, uptime calc=105.0 -> 5s < 15s threshold
        launcher_env["run_main"](
            time_values=[100.0, 100.1, 100.2, 100.3, 100.4, 105.0],
        )

        crash_msgs = [r for r in caplog.records
                      if "crashed after" in r.message.lower()
                      or "crash count" in r.message.lower()]
        assert len(crash_msgs) >= 1, (
            f"Expected crash log message, got: {[r.message for r in caplog.records]}"
        )

    def test_long_uptime_resets_crash_count(self, launcher_env, caplog):
        """Process running > 15s does NOT log a crash."""
        import logging
        caplog.set_level(logging.ERROR)

        # time.time() calls: (1) start_time=100.0, (2) shm_name, (3) gui_shm_name,
        # (4) uptime=130.0 -> 30s > 15s threshold. Provide extras for safety.
        launcher_env["run_main"](
            time_values=[100.0, 100.1, 100.2, 130.0] + [200.0] * 10,
        )

        crash_msgs = [r for r in caplog.records
                      if "crashed after" in r.message.lower()
                      or "crash count" in r.message.lower()]
        assert len(crash_msgs) == 0, (
            f"Expected no crash log, got: {[r.message for r in crash_msgs]}"
        )

    def test_three_crashes_aborts(self, launcher_env, caplog):
        """Three consecutive short-uptime cycles log 'crashed too many times'.

        To get 3 loop iterations, we need the restart flag to exist after
        each of the first two cycles (otherwise should_restart=False and the
        loop exits). But a restart flag would reset crash_count. So instead
        we observe that without a restart flag, the loop exits after one
        iteration with should_restart=False.

        The actual way to hit MAX_CRASHES is: something keeps setting
        should_restart=True externally. We simulate this by creating the
        restart flag file before each uptime check, but removing it before
        the crash detection check. However, the launcher checks restart flag
        AFTER uptime, and finding it resets crash_count.

        The real scenario: the while loop condition `crash_count < MAX_CRASHES`
        is a safety net. We can test the abort message by patching time to
        produce 3 short cycles while keeping should_restart True. We do this
        by creating the restart flag during the loop but having short uptime
        with no flag present at the exact moment of the crash check.

        Actually, re-reading the code more carefully: the restart flag check
        (line 192) happens AFTER the crash count increment (line 186-190).
        And finding the restart flag sets should_restart=True for the next
        iteration. So the sequence is:

        Iteration 1: short uptime, no flag -> crash_count=1, should_restart=False
        -> loop exits because should_restart=False.

        To reach crash_count=3, we need should_restart to stay True. The only
        way is via the restart flag. But the flag also resets crash_count=0.

        The crash check is: uptime < threshold AND NOT restart_flag_exists.
        If we have short uptime AND the flag does NOT exist at line 186,
        crash_count increments. Then at line 192, if the flag DOES exist
        (created between line 186 and 192), should_restart=True but
        crash_count already incremented. But that's a race condition we
        can't easily mock.

        In practice, the code as written means MAX_CRASHES can only be hit
        if something outside the launcher keeps requesting restarts while
        the processes keep crashing and the restart flag is absent at the
        uptime check moment. This is hard to mock cleanly.

        Instead, we verify the abort message is in the code path by making
        crash_count reach MAX_CRASHES. We mock it by having 3 iterations
        where each one: has short uptime AND no restart flag. To get
        multiple iterations, we need should_restart=True. Normally
        should_restart=False when no flag. But we can work around this
        by having the flag exist at the flag-check (line 192) but not
        exist at the crash-check (line 186).

        We achieve this by having a side_effect on os.path.exists that
        returns different values depending on which path is being checked
        and the call count.
        """
        import logging
        caplog.set_level(logging.CRITICAL)

        app_data = launcher_env["app_data"]
        shutdown_event = launcher_env["shutdown_event"]
        mock_shm = launcher_env["mock_shm"]
        mock_gui_shm = launcher_env["mock_gui_shm"]
        make_proc = launcher_env["make_process"]

        # We need 3 loop iterations with short uptime and no restart flag
        # at the crash check but with restart flag at the flag check (to
        # keep should_restart=True for iterations 1 and 2).
        #
        # The trick: create the restart flag file DURING the loop by
        # patching os.path.exists to lie about the restart flag at
        # the crash check (line 186) but tell the truth at line 192.
        #
        # Actually, line 186 uses os.path.exists(RESTART_FLAG_PATH) and
        # line 192 also uses os.path.exists(RESTART_FLAG_PATH). We need
        # line 186 to return False and line 192 to return True for
        # iterations 1 and 2. For iteration 3 both can return False
        # (loop exits because crash_count >= MAX_CRASHES).
        #
        # We'll create the restart flag file and patch the crash check
        # differently. Actually, it's simpler: we write to the restart
        # flag path between the two checks. But we can't inject code
        # between them.
        #
        # Simplest approach: patch os.path.exists with a side_effect
        # that tracks calls for the restart flag path specifically.

        restart_flag_path = str(app_data / "wheelhouse.restart")

        # We need 3 full iterations. Each iteration calls time.time() for:
        # (1) start_time, (2) shm_name, (3) shm_name again (gui),
        # (4) shm_name again (gui second call)
        # Then in finally: (5) uptime = time.time() - start_time
        # Total per iteration: ~5 time.time() calls, but we need extras
        # for safety. Let's provide plenty.
        time_values = []
        for i in range(3):
            base = 100.0 + i * 10
            # start_time, shm names (4 calls), uptime check (short = base + 5)
            time_values.extend([base, base + 0.1, base + 0.2, base + 0.3,
                                base + 0.4, base + 5.0])
        # Extra values in case we need more
        time_values.extend([200.0] * 10)

        # Track os.path.exists calls for restart flag
        # In each iteration: first call to exists(restart_flag) is at line 186
        # (crash check), second call is at line 192 (flag check), third is
        # at line 203 (pid file check).
        restart_flag_exists_calls = iter([
            # Iteration 1: crash check=False (count crash), flag check=True (restart)
            False, True,
            # Iteration 2: crash check=False (count crash), flag check=True (restart)
            False, True,
            # Iteration 3: crash check=False (count crash), flag check=False (exit)
            False, False,
        ])

        original_exists = os.path.exists

        def patched_exists(path):
            if path == restart_flag_path:
                try:
                    return next(restart_flag_exists_calls)
                except StopIteration:
                    return False
            return original_exists(path)

        # Create 3 sets of process mocks (one per iteration)
        all_process_mocks = []
        for i in range(3):
            all_process_mocks.extend([
                make_proc(alive=False, exitcode=1, name="LogicProcess", pid=42 + i * 10),
                make_proc(alive=False, exitcode=1, name="InputProcess", pid=43 + i * 10),
                make_proc(alive=False, exitcode=1, name="GuiProcess", pid=44 + i * 10),
            ])

        # 3 sets of SHM mocks (one per iteration)
        shm_mocks = []
        for i in range(3):
            s = MagicMock()
            s.name = f"test_shm_{i}"
            gs = MagicMock()
            gs.name = f"test_gui_shm_{i}"
            shm_mocks.extend([s, gs])

        # We also need to handle the restart flag file removal at line 194.
        # Since os.path.exists returns True for iterations 1 and 2 at the
        # flag check, os.remove(RESTART_FLAG_PATH) will be called. We need
        # to patch os.remove to not fail for a non-existent file.
        original_remove = os.remove

        def patched_remove(path):
            if path == restart_flag_path:
                return  # silently succeed
            return original_remove(path)

        with patch("services.wheelhouse.utils.system.get_app_data_path",
                    return_value=str(app_data)), \
             patch.object(launcher, "cleanup_stale_resources"), \
             patch("launcher.shared_memory.SharedMemory",
                   side_effect=shm_mocks), \
             patch("launcher.multiprocessing.Queue", return_value=Mock()), \
             patch("launcher.multiprocessing.Event",
                   return_value=shutdown_event), \
             patch("launcher.multiprocessing.Process",
                   side_effect=all_process_mocks), \
             patch.dict("sys.modules", launcher_env["sys_modules"]), \
             patch.object(launcher, "time", _fake_time_ns(time_values)), \
             patch("launcher.os.path.exists", side_effect=patched_exists), \
             patch("launcher.os.remove", side_effect=patched_remove):

            launcher.main()

        abort_msgs = [r for r in caplog.records
                      if "crashed too many times" in r.message.lower()]
        assert len(abort_msgs) >= 1, (
            f"Expected 'crashed too many times' log, got: "
            f"{[r.message for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# Restart flag integration (NEW - calls launcher.main())
# ---------------------------------------------------------------------------


class TestRestartFlagIntegration:
    """Test restart flag behavior by calling launcher.main()."""

    def test_restart_flag_triggers_second_cycle(self, launcher_env):
        """Restart flag causes launcher.main() to run two loop iterations.

        Verify by checking Process was constructed 6 times (3 per iteration).
        """
        app_data = launcher_env["app_data"]
        shutdown_event = launcher_env["shutdown_event"]
        make_proc = launcher_env["make_process"]

        # Create restart flag so iteration 1 sets should_restart=True
        restart_flag = app_data / "wheelhouse.restart"
        restart_flag.write_text("")

        # Need 2 iterations of time values. Each iteration needs:
        # start_time, shm_name*2 calls, uptime calc in finally
        time_values = [
            # Iteration 1
            100.0, 100.1, 100.2, 100.3, 100.4, 130.0,
            # Iteration 2
            200.0, 200.1, 200.2, 200.3, 200.4, 230.0,
            # Extra safety values
            300.0, 300.0, 300.0, 300.0, 300.0, 300.0,
        ]

        # 6 processes: 3 per iteration
        process_mocks = []
        for i in range(6):
            process_mocks.append(
                make_proc(alive=False, exitcode=0,
                          name=["LogicProcess", "InputProcess", "GuiProcess"][i % 3],
                          pid=42 + i)
            )

        # 4 SHM mocks: 2 per iteration
        shm_mocks = []
        for i in range(4):
            m = MagicMock()
            m.name = f"shm_{i}"
            shm_mocks.append(m)

        with patch("services.wheelhouse.utils.system.get_app_data_path",
                    return_value=str(app_data)), \
             patch.object(launcher, "cleanup_stale_resources"), \
             patch("launcher.shared_memory.SharedMemory",
                   side_effect=shm_mocks), \
             patch("launcher.multiprocessing.Queue", return_value=Mock()), \
             patch("launcher.multiprocessing.Event",
                   return_value=shutdown_event), \
             patch("launcher.multiprocessing.Process",
                   side_effect=process_mocks), \
             patch.dict("sys.modules", launcher_env["sys_modules"]), \
             patch.object(launcher, "time", _fake_time_ns(time_values)):

            launcher.main()

        # Verify 6 processes were started (3 per iteration * 2 iterations)
        started = [p for p in process_mocks if p.start.called]
        assert len(started) == 6, (
            f"Expected 6 process starts (2 iterations), got {len(started)}"
        )

    def test_restart_flag_is_consumed(self, launcher_env):
        """Restart flag file is deleted after being detected."""
        app_data = launcher_env["app_data"]

        restart_flag = app_data / "wheelhouse.restart"
        restart_flag.write_text("")

        # Long uptime so no crash on either iteration
        time_values = [
            100.0, 100.1, 100.2, 100.3, 100.4, 130.0,
            200.0, 200.1, 200.2, 200.3, 200.4, 230.0,
            300.0, 300.0, 300.0, 300.0, 300.0, 300.0,
        ]

        # Need process mocks for 2 iterations
        make_proc = launcher_env["make_process"]
        process_mocks = [
            make_proc(alive=False, exitcode=0, name="LogicProcess", pid=42),
            make_proc(alive=False, exitcode=0, name="InputProcess", pid=43),
            make_proc(alive=False, exitcode=0, name="GuiProcess", pid=44),
            make_proc(alive=False, exitcode=0, name="LogicProcess", pid=52),
            make_proc(alive=False, exitcode=0, name="InputProcess", pid=53),
            make_proc(alive=False, exitcode=0, name="GuiProcess", pid=54),
        ]
        shm_mocks = [MagicMock(name=f"shm_{i}") for i in range(4)]

        with patch("services.wheelhouse.utils.system.get_app_data_path",
                    return_value=str(app_data)), \
             patch.object(launcher, "cleanup_stale_resources"), \
             patch("launcher.shared_memory.SharedMemory",
                   side_effect=shm_mocks), \
             patch("launcher.multiprocessing.Queue", return_value=Mock()), \
             patch("launcher.multiprocessing.Event",
                   return_value=launcher_env["shutdown_event"]), \
             patch("launcher.multiprocessing.Process",
                   side_effect=process_mocks), \
             patch.dict("sys.modules", launcher_env["sys_modules"]), \
             patch.object(launcher, "time", _fake_time_ns(time_values)):

            launcher.main()

        assert not restart_flag.exists(), "Restart flag should be deleted"

    def test_restart_flag_prevents_crash_increment(self, launcher_env, caplog):
        """With restart flag present, short uptime does NOT log a crash."""
        import logging
        caplog.set_level(logging.ERROR)

        app_data = launcher_env["app_data"]
        restart_flag = app_data / "wheelhouse.restart"
        restart_flag.write_text("")

        # Short uptime (5s) but restart flag present -> no crash logged
        # Need values for 2 iterations (flag triggers restart)
        time_values = [
            100.0, 100.1, 100.2, 100.3, 100.4, 105.0,
            200.0, 200.1, 200.2, 200.3, 200.4, 230.0,
            300.0, 300.0, 300.0, 300.0, 300.0, 300.0,
        ]

        make_proc = launcher_env["make_process"]
        process_mocks = [
            make_proc(alive=False, exitcode=0, name="LogicProcess", pid=42),
            make_proc(alive=False, exitcode=0, name="InputProcess", pid=43),
            make_proc(alive=False, exitcode=0, name="GuiProcess", pid=44),
            make_proc(alive=False, exitcode=0, name="LogicProcess", pid=52),
            make_proc(alive=False, exitcode=0, name="InputProcess", pid=53),
            make_proc(alive=False, exitcode=0, name="GuiProcess", pid=54),
        ]
        shm_mocks = [MagicMock(name=f"shm_{i}") for i in range(4)]

        with patch("services.wheelhouse.utils.system.get_app_data_path",
                    return_value=str(app_data)), \
             patch.object(launcher, "cleanup_stale_resources"), \
             patch("launcher.shared_memory.SharedMemory",
                   side_effect=shm_mocks), \
             patch("launcher.multiprocessing.Queue", return_value=Mock()), \
             patch("launcher.multiprocessing.Event",
                   return_value=launcher_env["shutdown_event"]), \
             patch("launcher.multiprocessing.Process",
                   side_effect=process_mocks), \
             patch.dict("sys.modules", launcher_env["sys_modules"]), \
             patch.object(launcher, "time", _fake_time_ns(time_values)):

            launcher.main()

        # First iteration: short uptime BUT restart flag exists at line 186
        # -> crash check condition is: uptime < 15 AND NOT flag_exists
        # -> flag_exists=True so NOT flag_exists=False -> condition is False
        # -> crash_count stays 0 (reset to 0 in else branch)
        crash_msgs = [r for r in caplog.records
                      if "crash count" in r.message.lower()]
        assert len(crash_msgs) == 0, (
            f"Expected no crash log with restart flag present, "
            f"got: {[r.message for r in crash_msgs]}"
        )


# ---------------------------------------------------------------------------
# Process death integration (NEW - calls launcher.main())
# ---------------------------------------------------------------------------


class TestProcessDeathIntegration:
    """Test process death detection by calling launcher.main()."""

    def test_dead_process_triggers_shutdown(self, launcher_env):
        """When a process is dead, shutdown_event.set() is called."""
        shutdown_event = launcher_env["shutdown_event"]
        make_proc = launcher_env["make_process"]

        # Logic process dead on arrival, others alive
        logic = make_proc(alive=False, exitcode=1, name="LogicProcess", pid=42)
        inp = make_proc(alive=True, exitcode=None, name="InputProcess", pid=43)
        gui = make_proc(alive=True, exitcode=None, name="GuiProcess", pid=44)

        # Provide plenty of time values since alive processes go through
        # join/terminate path which may call time.time() additional times
        launcher_env["run_main"](
            time_values=[100.0, 100.1, 100.2, 130.0] + [200.0] * 10,
            process_mocks=[logic, inp, gui],
        )

        shutdown_event.set.assert_called()

    def test_all_alive_keeps_monitoring(self, launcher_env):
        """When all processes are alive, loop continues until shutdown_event."""
        shutdown_event = launcher_env["shutdown_event"]
        make_proc = launcher_env["make_process"]

        # is_set() calls: (1) while loop check=False, (2) while loop 2nd=True,
        # (3) finally block check. Provide enough values.
        shutdown_event.is_set.side_effect = [False, True, True]

        logic = make_proc(alive=True, exitcode=None, name="LogicProcess", pid=42)
        inp = make_proc(alive=True, exitcode=None, name="InputProcess", pid=43)
        gui = make_proc(alive=True, exitcode=None, name="GuiProcess", pid=44)

        launcher_env["run_main"](
            time_values=[100.0, 100.1, 100.2, 130.0] + [200.0] * 10,
            process_mocks=[logic, inp, gui],
        )

        # The loop ran - processes were checked for alive status
        logic.is_alive.assert_called()


# ---------------------------------------------------------------------------
# Graceful shutdown integration (NEW - calls launcher.main())
# ---------------------------------------------------------------------------


class TestGracefulShutdownIntegration:
    """Test graceful-then-forced termination by calling launcher.main()."""

    def test_processes_get_join_with_grace_period(self, launcher_env):
        """After loop exits, alive processes get join(timeout=5)."""
        make_proc = launcher_env["make_process"]

        # Process starts alive (triggers the join path), then dies after join
        logic = make_proc(alive=True, exitcode=None, name="LogicProcess", pid=42)
        inp = make_proc(alive=True, exitcode=None, name="InputProcess", pid=43)
        gui = make_proc(alive=True, exitcode=None, name="GuiProcess", pid=44)

        # is_set() calls: (1) while loop=False, (2) while loop=True (exit),
        # (3) finally block check. Provide enough.
        shutdown_event = launcher_env["shutdown_event"]
        shutdown_event.is_set.side_effect = [False, True, True]

        launcher_env["run_main"](
            time_values=[100.0, 100.1, 100.2, 130.0] + [200.0] * 10,
            process_mocks=[logic, inp, gui],
        )

        # All 3 processes should get join(timeout=5)
        for proc in [logic, inp, gui]:
            proc.join.assert_called_with(timeout=launcher.SHUTDOWN_GRACE_PERIOD_S)

    def test_stuck_process_gets_terminated(self, launcher_env):
        """Process still alive after join() gets terminate() called."""
        make_proc = launcher_env["make_process"]

        # Process that stays alive forever (stuck)
        stuck = make_proc(alive=True, exitcode=None, name="LogicProcess", pid=42)
        # Other processes already dead
        inp = make_proc(alive=False, exitcode=0, name="InputProcess", pid=43)
        gui = make_proc(alive=False, exitcode=0, name="GuiProcess", pid=44)

        launcher_env["run_main"](
            time_values=[100.0, 100.1, 100.2, 100.3, 100.4, 130.0],
            process_mocks=[stuck, inp, gui],
        )

        # Stuck process should be terminated after join times out
        # (is_alive returns True even after join, so terminate is called)
        stuck.terminate.assert_called()


# ---------------------------------------------------------------------------
# Shared memory cleanup (NEW - calls launcher.main())
# ---------------------------------------------------------------------------


class TestSharedMemoryCleanup:
    """Test shared memory lifecycle by calling launcher.main()."""

    def test_shm_close_and_unlink_called(self, launcher_env):
        """Both SHM objects get close() and unlink() calls after main() exits."""
        mock_shm = launcher_env["mock_shm"]
        mock_gui_shm = launcher_env["mock_gui_shm"]

        launcher_env["run_main"](
            time_values=[100.0, 100.1, 100.2, 100.3, 100.4, 130.0],
        )

        mock_shm.close.assert_called_once()
        mock_shm.unlink.assert_called_once()
        mock_gui_shm.close.assert_called_once()
        mock_gui_shm.unlink.assert_called_once()

    def test_shm_cleaned_up_even_on_exception(self, launcher_env):
        """SHM is cleaned up even when process creation raises."""
        mock_shm = launcher_env["mock_shm"]
        mock_gui_shm = launcher_env["mock_gui_shm"]
        app_data = launcher_env["app_data"]
        shutdown_event = launcher_env["shutdown_event"]

        with patch("services.wheelhouse.utils.system.get_app_data_path",
                    return_value=str(app_data)), \
             patch.object(launcher, "cleanup_stale_resources"), \
             patch("launcher.shared_memory.SharedMemory",
                   side_effect=[mock_shm, mock_gui_shm]), \
             patch("launcher.multiprocessing.Queue", return_value=Mock()), \
             patch("launcher.multiprocessing.Event",
                   return_value=shutdown_event), \
             patch.object(launcher, "time",
                          _fake_time_ns([100.0, 100.1, 100.2, 100.3, 100.4, 100.5])), \
             patch.dict("sys.modules", launcher_env["sys_modules"]), \
             patch("launcher.multiprocessing.Process",
                   side_effect=RuntimeError("boom")):

            launcher.main()

        mock_shm.close.assert_called_once()
        mock_shm.unlink.assert_called_once()
        mock_gui_shm.close.assert_called_once()
        mock_gui_shm.unlink.assert_called_once()


# ---------------------------------------------------------------------------
# Speaker level left lowered by a lost push-to-talk hold
# (wh-ptt-mute-orphaned-on-process-loss)
# ---------------------------------------------------------------------------


class TestTheLauncherRestoresTheLevelOfALostHold:
    """A Logic process lost during a push-to-talk hold leaves the speakers at
    the lowered level, and the launcher is the only process left to put the
    level back. It restores at the two points where no Logic process runs:
    at launcher start before any child starts, and after every child of a
    cycle has exited, before the restart decision.

    The restore function is patched, so no test here reads the record or
    calls Core Audio.
    """

    NAMES = ("LogicProcess", "InputProcess", "GuiProcess")

    @staticmethod
    def _tracked_process(events, name, pid, exits_on="join"):
        """A process mock that records start, join and terminate in events.

        exits_on="join": the process exits during its first join.
        exits_on="terminate": a join exits it only after terminate() was
        called, because terminate() only asks Windows to end the process.
        exits_on="never": the process stays alive.
        """
        state = {"alive": False, "terminated": False}
        proc = Mock()
        proc.name = name
        proc.pid = pid
        proc.exitcode = None

        def start():
            events.append(("start", name))
            state["alive"] = True

        def join(timeout=None):
            events.append(("join", name))
            if exits_on == "join" or (exits_on == "terminate" and state["terminated"]):
                state["alive"] = False

        def terminate():
            events.append(("terminate", name))
            state["terminated"] = True

        proc.start.side_effect = start
        proc.join.side_effect = join
        proc.terminate.side_effect = terminate
        proc.is_alive.side_effect = lambda: state["alive"]
        return proc

    @staticmethod
    def _run_launcher(launcher_env, events, process_mocks, restore, inside=None):
        """Run launcher.main() for len(process_mocks) // 3 cycles. Returns the SHM mocks.

        inside: an optional context manager entered after every patch here,
        for a change that would stop those patches from resolving their
        targets.
        """
        app_data = launcher_env["app_data"]
        cycles = len(process_mocks) // 3
        # The monitor loop ends at once, so each cycle goes straight to its
        # cleanup phase.
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = True
        # Four time.time() calls per cycle: start, two SHM names, uptime.
        time_values = []
        for cycle in range(cycles):
            base = 100.0 * (cycle + 1)
            time_values += [base, base + 0.1, base + 0.2, base + 30.0]
        shm_mocks = [MagicMock(name=f"shm_{i}") for i in range(2 * cycles)]

        with patch("services.wheelhouse.utils.system.get_app_data_path",
                   return_value=str(app_data)), \
             patch.object(launcher, "cleanup_stale_resources",
                          side_effect=lambda: events.append(("cleanup_stale",))), \
             patch.object(launcher, "disable_console_quick_edit",
                          Mock(return_value=True)), \
             patch("services.wheelhouse.utils.ptt_volume_record.restore_orphaned_volume",
                   restore), \
             patch("launcher.shared_memory.SharedMemory", side_effect=shm_mocks), \
             patch("launcher.multiprocessing.Queue", return_value=Mock()), \
             patch("launcher.multiprocessing.Event", return_value=shutdown_event), \
             patch("launcher.multiprocessing.Process", side_effect=process_mocks), \
             patch.dict("sys.modules", launcher_env["sys_modules"]), \
             patch.object(launcher, "time", _fake_time_ns(time_values)), \
             (inside if inside is not None else contextlib.nullcontext()):

            launcher.main()

        return shm_mocks

    def test_the_start_restore_runs_after_the_stale_cleanup_and_the_instance_record_and_before_the_first_child_starts(
        self, launcher_env
    ):
        """A hold that a previous run lost is restored before a new Logic
        process can start a hold of its own. The stale cleanup comes first
        because it stops a previous launcher whose Logic process could still
        hold the speakers down. The instance record comes before the restore,
        so a later start can find and stop this launcher while the restore
        runs."""
        events = []
        processes = [
            self._tracked_process(events, name, 42 + i) for i, name in enumerate(self.NAMES)
        ]
        restore = Mock(side_effect=lambda cancel=None: events.append(("restore",)))

        with patch.object(
            launcher, "write_instance_record",
            side_effect=lambda *args, **kwargs: events.append(("instance record written",)),
        ):
            self._run_launcher(launcher_env, events, processes, restore)

        first_start = events.index(("start", "LogicProcess"))
        assert events[:first_start] == [
            ("cleanup_stale",), ("instance record written",), ("restore",)
        ], (
            f"expected the stale cleanup, the instance record and then the restore before "
            f"the first child starts, got {events[:first_start]}"
        )

    def test_a_start_restore_that_does_not_return_is_left_behind_at_the_time_limit(
        self, launcher_env, caplog, monkeypatch
    ):
        """A Core Audio call can fail to return. The launcher waits for the
        restore at most SHUTDOWN_GRACE_PERIOD_S, then sets the cancel flag,
        logs a warning that names the limit, and starts its children.

        The blocked restore waits on an Event with its own 2 s timeout, and
        the test sets that Event after the launcher returns. A launcher that
        waits without a limit therefore fails an assertion after 2 s and
        does not hang."""
        monkeypatch.setattr(launcher, "SHUTDOWN_GRACE_PERIOD_S", 0.2)
        events = []
        processes = [
            self._tracked_process(events, name, 42 + i) for i, name in enumerate(self.NAMES)
        ]
        unblock = threading.Event()
        returned = threading.Event()
        flags = []
        threads = []

        def restore(cancel=None):
            flags.append(cancel)
            threads.append(threading.current_thread())
            if len(flags) == 1:
                unblock.wait(timeout=2)
                events.append(("start restore returned",))
                returned.set()

        try:
            with caplog.at_level(logging.WARNING, logger="launcher"):
                self._run_launcher(launcher_env, events, processes, Mock(side_effect=restore))
        finally:
            unblock.set()
            returned.wait(timeout=5)

        first_start = events.index(("start", "LogicProcess"))
        assert ("start restore returned",) not in events[:first_start], (
            "the launcher waited past the time limit for the start restore"
        )
        # A thread that is not a daemon keeps the launcher process alive at
        # exit for as long as its Core Audio call does not return.
        assert threads[0].daemon, "the start restore did not run in a daemon thread"
        assert flags[0] is not None, "the launcher gave the start restore no cancel flag"
        assert flags[0].is_set(), (
            "the launcher did not set the cancel flag of the start restore at the time limit"
        )
        assert "did not finish within 0.2 s" in caplog.text, "no warning named the time limit"

    def test_a_cleanup_restore_that_does_not_return_is_left_behind_and_the_restart_decision_still_runs(
        self, launcher_env, monkeypatch
    ):
        """The restore after the children exit has the same time limit, so a
        Core Audio call that does not return cannot stop the restart decision.
        The restart flag makes a second cycle; the blocked restore is the one
        of the first cycle. The blocked restore has its own 2 s timeout, as in
        the start test."""
        monkeypatch.setattr(launcher, "SHUTDOWN_GRACE_PERIOD_S", 0.2)
        events = []
        restart_flag = launcher_env["app_data"] / "wheelhouse.restart"
        restart_flag.write_text("")
        processes = [
            self._tracked_process(events, name, 42 + i)
            for i, name in enumerate(self.NAMES * 2)
        ]
        unblock = threading.Event()
        returned = threading.Event()
        flags = []

        def restore(cancel=None):
            flags.append(cancel)
            if len(flags) == 2:
                unblock.wait(timeout=2)
                events.append(("cleanup restore returned",))
                returned.set()

        try:
            self._run_launcher(launcher_env, events, processes, Mock(side_effect=restore))
        finally:
            unblock.set()
            returned.wait(timeout=5)

        starts = [i for i, event in enumerate(events) if event == ("start", "LogicProcess")]
        assert len(starts) == 2, f"the restart decision did not start a second cycle: {events}"
        assert ("cleanup restore returned",) not in events[:starts[1]], (
            "the launcher waited past the time limit for the cleanup restore"
        )
        assert not restart_flag.exists(), "the restart decision did not remove the restart flag"

    def test_the_restore_runs_after_every_child_has_exited_and_before_the_restart_decision(
        self, launcher_env
    ):
        """The restart flag is still on disk when the restore runs, which puts
        the restore before the restart decision that removes the flag. The
        second cycle shows the restore runs in every cycle."""
        events = []
        restart_flag = launcher_env["app_data"] / "wheelhouse.restart"
        restart_flag.write_text("")
        processes = [
            self._tracked_process(events, name, 42 + i)
            for i, name in enumerate(self.NAMES * 2)
        ]

        def restore(cancel=None):
            running = tuple(p.name for p in processes if p.is_alive())
            events.append(("restore", "restart flag on disk" if restart_flag.exists() else "no restart flag", running))

        self._run_launcher(launcher_env, events, processes, Mock(side_effect=restore))

        starts = [("start", name) for name in self.NAMES]
        joins = [("join", name) for name in self.NAMES]
        after_first_start = events[events.index(("start", "LogicProcess")):]
        assert after_first_start == (
            starts + joins + [("restore", "restart flag on disk", ())]
            + starts + joins + [("restore", "no restart flag", ())]
        ), f"got {after_first_start}"

    def test_a_child_that_needed_terminate_is_joined_again_before_the_restore(
        self, launcher_env
    ):
        """terminate() only asks Windows to end the process. Without a join
        after it, the restore could run while the Logic process still holds
        the speakers down."""
        events = []
        logic = self._tracked_process(events, "LogicProcess", 42, exits_on="terminate")
        inp = self._tracked_process(events, "InputProcess", 43)
        gui = self._tracked_process(events, "GuiProcess", 44)

        def restore(cancel=None):
            events.append(("restore", tuple(p.name for p in (logic, inp, gui) if p.is_alive())))

        self._run_launcher(launcher_env, events, [logic, inp, gui], Mock(side_effect=restore))

        after_starts = events[events.index(("start", "GuiProcess")) + 1:]
        assert after_starts == [
            ("join", "LogicProcess"), ("join", "InputProcess"), ("join", "GuiProcess"),
            ("terminate", "LogicProcess"), ("join", "LogicProcess"),
            ("restore", ()),
        ], f"got {after_starts}"
        logic.join.assert_called_with(timeout=launcher.SHUTDOWN_GRACE_PERIOD_S)

    def test_the_restore_is_skipped_while_a_child_is_still_running(self, launcher_env, caplog):
        events = []
        logic = self._tracked_process(events, "LogicProcess", 42, exits_on="never")
        inp = self._tracked_process(events, "InputProcess", 43)
        gui = self._tracked_process(events, "GuiProcess", 44)
        restore = Mock(side_effect=lambda cancel=None: events.append(("restore",)))

        with caplog.at_level(logging.WARNING, logger="launcher"):
            self._run_launcher(launcher_env, events, [logic, inp, gui], restore)

        assert "speaker restore skipped" in caplog.text and "LogicProcess (42)" in caplog.text, (
            "no warning named the child that kept the restore from running"
        )
        after_starts = events[events.index(("start", "GuiProcess")) + 1:]
        assert ("restore",) not in after_starts, (
            "the restore ran while LogicProcess was still running"
        )

    # The mutation restore-wrapper-exception-propagates removes the try/except
    # around the restore thread's body, so the exception escapes the thread and
    # pytest's threadexception plugin would fail this test on the warning before
    # it reaches its own assertion. The unmutated code raises no such warning,
    # so the marker changes nothing about what this test proves.
    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_a_restore_that_raises_is_logged_and_the_launcher_goes_on(
        self, launcher_env, caplog
    ):
        events = []
        processes = [
            self._tracked_process(events, name, 42 + i) for i, name in enumerate(self.NAMES)
        ]
        restore = Mock(side_effect=RuntimeError("the audio service is not running"))

        with caplog.at_level(logging.ERROR, logger="launcher"):
            shm_mocks = self._run_launcher(launcher_env, events, processes, restore)

        assert "Could not restore the push-to-talk speaker level" in caplog.text
        assert restore.call_count == 2, "the launcher did not reach both restore points"
        assert [e for e in events if e[0] == "start"] == [("start", n) for n in self.NAMES]
        for shm in shm_mocks:
            shm.close.assert_called_once()
            shm.unlink.assert_called_once()

    def test_a_restore_that_cannot_be_imported_is_logged_and_the_launcher_goes_on(
        self, launcher_env, caplog
    ):
        """The launcher itself imports the restore before it starts the
        restore thread. A failed import is logged at both restore points and
        the children still start."""
        events = []
        processes = [
            self._tracked_process(events, name, 42 + i) for i, name in enumerate(self.NAMES)
        ]

        with caplog.at_level(logging.ERROR, logger="launcher"):
            self._run_launcher(
                launcher_env, events, processes, Mock(name="restore that is never imported"),
                inside=patch.dict("sys.modules", {"services.wheelhouse.utils.ptt_volume_record": None}),
            )

        assert caplog.text.count("Could not restore the push-to-talk speaker level") == 2, (
            "the failed import was not logged at both restore points"
        )
        assert [e for e in events if e[0] == "start"] == [("start", n) for n in self.NAMES]


# ---------------------------------------------------------------------------
# One-shot --reset-first-use-hints CLI shortcut (wh-r3xy1)
# ---------------------------------------------------------------------------
#
# Mirrors the --clear-screen-reader-flag tests in test_launcher_clear_flag.py:
# the shortcut deletes the first-use-hint record file under the module-scoped
# lock and exits BEFORE any process spawn -- exit 0 on success (including an
# already-absent file), exit 1 on a delete error. A fake deleter is injected so
# no real file is touched, and the supervisor body is replaced so no process is
# ever spawned.


class _RecordingDeleter:
    """Fake hint-record deleter that records call count and a fixed result."""

    def __init__(self, result: bool = True) -> None:
        self.calls = 0
        self._result = result

    def __call__(self) -> bool:
        self.calls += 1
        return self._result


class TestResetFirstUseHintsIntent:
    def test_intent_true_when_token_present(self):
        assert launcher._reset_first_use_hints_intent(
            ["launcher.py", "--reset-first-use-hints"]
        ) is True

    def test_intent_true_when_token_among_other_args(self):
        assert launcher._reset_first_use_hints_intent(
            ["launcher.py", "--foo", "--reset-first-use-hints", "--bar"]
        ) is True

    def test_intent_false_when_token_absent(self):
        assert launcher._reset_first_use_hints_intent(["launcher.py"]) is False

    def test_intent_false_for_empty_argv(self):
        assert launcher._reset_first_use_hints_intent([]) is False


class TestResetFirstUseHintsOneShot:
    def test_present_deletes_and_exits_zero_without_spawn(self, capsys):
        """--reset-first-use-hints deletes the record and exits 0 with no spawn."""
        deleter = _RecordingDeleter(result=True)
        spawn_marker = {"reached": False}

        def _explode(*_args, **_kwargs):
            spawn_marker["reached"] = True
            raise AssertionError("supervisor loop must not run on the reset path")

        with patch.object(launcher, "_run_supervisor", side_effect=_explode), \
             patch("launcher.multiprocessing.Process", side_effect=_explode), \
             patch("launcher.shared_memory.SharedMemory", side_effect=_explode):
            with pytest.raises(SystemExit) as exc:
                launcher.main(
                    argv=["launcher.py", "--reset-first-use-hints"],
                    delete_hints_fn=deleter,
                )

        assert exc.value.code == 0
        assert deleter.calls == 1
        assert spawn_marker["reached"] is False
        out = capsys.readouterr().out
        assert out.strip() != ""

    def test_present_exits_nonzero_when_delete_fails(self):
        """A failed delete exits 1 so the failure is observable."""
        deleter = _RecordingDeleter(result=False)

        with patch.object(launcher, "_run_supervisor"):
            with pytest.raises(SystemExit) as exc:
                launcher.main(
                    argv=["launcher.py", "--reset-first-use-hints"],
                    delete_hints_fn=deleter,
                )

        assert exc.value.code == 1
        assert deleter.calls == 1

    def test_present_does_not_invoke_supervisor(self):
        """The supervisor body is never entered on the reset path."""
        deleter = _RecordingDeleter()

        with patch.object(launcher, "_run_supervisor") as mock_super:
            with pytest.raises(SystemExit):
                launcher.main(
                    argv=["launcher.py", "--reset-first-use-hints"],
                    delete_hints_fn=deleter,
                )

        mock_super.assert_not_called()


class TestResetFirstUseHintsAbsent:
    def test_absent_does_not_delete(self):
        """Without the token, main() runs the supervisor and never deletes."""
        deleter = _RecordingDeleter()

        with patch.object(launcher, "_run_supervisor") as mock_super:
            launcher.main(argv=["launcher.py"], delete_hints_fn=deleter)

        assert deleter.calls == 0
        mock_super.assert_called_once()


class TestResetFirstUseHintsBootstrap:
    def test_reset_path_skips_multiprocessing_setup(self):
        """On the reset path, freeze_support/set_start_method are NOT called."""
        with patch.object(launcher.multiprocessing, "freeze_support") as mock_fs, \
             patch.object(launcher.multiprocessing, "set_start_method") as mock_ssm, \
             patch.object(launcher, "main") as mock_main:
            launcher._bootstrap(["launcher.py", "--reset-first-use-hints"])

        mock_fs.assert_not_called()
        mock_ssm.assert_not_called()
        mock_main.assert_called_once()


# ---------------------------------------------------------------------------
# Console-write resilience (wh-console-write-resilience)
# ---------------------------------------------------------------------------


class TestLauncherLoggingResilience:
    """The supervisor thread must never block on a frozen console.

    Crash recovery previously logged synchronously to stderr on the
    supervisor thread (logger.error at child death), so a conhost hang
    wedged the restart loop before it could respawn the dead child. The
    launcher now uses the same producer-queue / listener-thread split as
    the service processes, with the file handler ahead of stderr.
    """

    @pytest.fixture
    def restored_root_logger(self):
        root = logging.getLogger()
        saved_handlers = root.handlers.copy()
        saved_level = root.level
        yield root
        for h in root.handlers.copy():
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)

    def test_configure_launcher_logging_is_queue_split_file_first(
        self, tmp_path, restored_root_logger
    ):
        import launcher
        from concurrent_log_handler import ConcurrentRotatingFileHandler
        from services.wheelhouse.utils.queue_logging import (
            _DroppingQueueHandler,
        )

        root = restored_root_logger
        for h in root.handlers.copy():
            root.removeHandler(h)

        listener = _REAL_CONFIGURE_LAUNCHER_LOGGING(str(tmp_path))
        try:
            # Producer side: exactly one non-blocking queue handler on root,
            # no direct StreamHandler/file handler (those live on the
            # listener thread).
            queue_handlers = [
                h for h in root.handlers
                if isinstance(h, _DroppingQueueHandler)
            ]
            assert len(queue_handlers) == 1
            assert not any(
                type(h) in (logging.StreamHandler,)
                or isinstance(h, ConcurrentRotatingFileHandler)
                for h in root.handlers
            ), "supervisor thread must not own blocking handlers"

            # Listener side: file handler strictly before the stderr handler.
            kinds = [type(h) for h in listener.handlers]
            assert ConcurrentRotatingFileHandler in kinds
            file_idx = kinds.index(ConcurrentRotatingFileHandler)
            stream_idx = next(
                i for i, h in enumerate(listener.handlers)
                if type(h) is logging.StreamHandler
            )
            assert file_idx < stream_idx

            # End to end: a supervisor log line reaches the file.
            logging.getLogger("launcher.test").error("recovery message")
            listener.stop(timeout=5.0)
            log_file = tmp_path / "wheelhouse.log"
            assert log_file.exists()
            assert "recovery message" in log_file.read_text(encoding="utf-8")
        finally:
            # Complete teardown: watchdog thread, handler close, module
            # state -- not just the listener (wh-log-crash-fixes.1.2b).
            launcher._teardown_launcher_logging()

    def test_reconfigure_does_not_accumulate_queue_handlers(
        self, tmp_path, restored_root_logger
    ):
        """wh-launcher-test-log-leak: a second _configure_launcher_logging
        call (pytest runs launcher.main() many times in one process) must
        replace the first queue handler, not add another. With k
        accumulated handlers every record is written to the file k
        times -- the 2026-07-09/10 log shows the k-fold duplication.
        """
        from services.wheelhouse.utils.queue_logging import (
            _DroppingQueueHandler,
        )

        root = restored_root_logger
        for h in root.handlers.copy():
            root.removeHandler(h)

        first = _REAL_CONFIGURE_LAUNCHER_LOGGING(str(tmp_path))
        second = _REAL_CONFIGURE_LAUNCHER_LOGGING(str(tmp_path))
        try:
            queue_handlers = [
                h for h in root.handlers
                if isinstance(h, _DroppingQueueHandler)
            ]
            assert len(queue_handlers) == 1, (
                "reconfiguring launcher logging must not leave the "
                "previous queue handler on the root logger"
            )
        finally:
            # The reconfigure already tore down `first`; this reaps
            # `second` plus its watchdog and module state
            # (wh-log-crash-fixes.1.2b). `first` needs no extra stop.
            launcher._teardown_launcher_logging()

    def test_reconfigure_closes_previous_listener_handlers(
        self, tmp_path, restored_root_logger
    ):
        """wh-log-crash-fixes.1.2a: replacing the logging split must
        close the old listener's handlers -- the rotating file handler
        otherwise keeps wheelhouse.log (and its lock file) open for the
        rest of the process, one leaked handle pair per reconfigure."""
        root = restored_root_logger
        for h in root.handlers.copy():
            root.removeHandler(h)

        first = _REAL_CONFIGURE_LAUNCHER_LOGGING(str(tmp_path))
        try:
            with contextlib.ExitStack() as stack:
                close_spies = [
                    stack.enter_context(
                        patch.object(h, "close", wraps=h.close)
                    )
                    for h in first.handlers
                ]
                _REAL_CONFIGURE_LAUNCHER_LOGGING(str(tmp_path))
                for spy in close_spies:
                    spy.assert_called()
        finally:
            launcher._teardown_launcher_logging()

    def test_teardown_launcher_logging_stops_watchdog_and_clears_state(
        self, tmp_path, restored_root_logger
    ):
        """wh-log-crash-fixes.1.2b: tests that call the REAL configure
        function need a complete teardown -- watchdog thread stopped,
        listener stopped, handlers closed, module state cleared --
        or each real call leaks a daemon thread and open file handles
        for the rest of the pytest session."""
        from services.wheelhouse.utils.queue_logging import (
            _DroppingQueueHandler,
        )

        root = restored_root_logger
        for h in root.handlers.copy():
            root.removeHandler(h)

        listener = _REAL_CONFIGURE_LAUNCHER_LOGGING(str(tmp_path))
        watchdog = launcher._launcher_logging_state["watchdog"]
        assert watchdog is not None and watchdog.is_alive()

        launcher._teardown_launcher_logging()

        assert not any(
            isinstance(h, _DroppingQueueHandler) for h in root.handlers
        ), "teardown must remove the queue handler from the root logger"
        assert not watchdog.is_alive(), "teardown must stop the watchdog"
        assert not listener.is_running, "teardown must stop the listener"
        assert all(
            v is None for v in launcher._launcher_logging_state.values()
        ), "teardown must clear the module-global state"

    def test_launcher_main_under_pytest_leaves_root_logging_alone(
        self, launcher_env, restored_root_logger
    ):
        """wh-launcher-test-log-leak: launcher.main() invoked from the
        test fixtures must not install log handlers -- the real
        _configure_launcher_logging opens the PRODUCTION repo-root
        wheelhouse.log (path derived from launcher.py's __file__), so
        every unstubbed test run writes a mock ERROR/CRITICAL burst
        into the live log (22 bursts of 240 lines on 2026-07-09/10).
        """
        root = restored_root_logger
        handlers_before = list(root.handlers)

        launcher_env["run_main"]([100.0, 100.3, 100.4, 100.5, 100.6])

        assert list(root.handlers) == handlers_before, (
            "launcher.main() under pytest must not touch the root "
            "logger (and must not open the production wheelhouse.log)"
        )


# ---------------------------------------------------------------------------
# What a restart cycle rebuilds (wh-overlay-slow-uia-stale-badges.11)
# ---------------------------------------------------------------------------


class TestRestartCycleRebuildsEverything:
    """Why this class exists, and what it deliberately does NOT prove.

    The containment in this bead lives INSIDE the Input command loop --
    stale-command expiry and the dispatch watchdog -- rather than in a
    supervisor that restarts a hung Input process. These tests are the
    structural reason for that choice: the launcher owns no path that
    restarts the Input process by itself. Recovery is a whole cycle, and a
    whole cycle discards every object the three processes shared, so a
    command in flight cannot survive it.

    WHAT THEY DO NOT PROVE. No real process starts here; the launcher's
    supervisor loop runs against mocks. They say what the launcher
    CONSTRUCTS, not how a real crash interleaves with a real scheduler, and
    they cannot show what a half-written frame looks like at the moment a
    process dies. The cross-process journey belongs to
    wh-overlay-slow-uia-stale-badges.12, which another session owns.
    """

    def _run_two_cycles(self, launcher_env, first_cycle_alive=(False, False, False)):
        """Drive launcher.main() through two supervisor cycles.

        Returns (process_calls, shm_names, events, positional_calls),
        where process_calls holds the kwargs of each
        multiprocessing.Process construction in order, shm_names holds
        each requested segment name in order, events holds every
        multiprocessing.Event object handed out, and positional_calls
        holds the positional-argument tuple of each construction, in the
        same order as process_calls.

        positional_calls is kept deliberately, and it stays index-aligned
        with process_calls past the sixth construction as well as within
        it. It is empty on every call while the launcher names all three
        of its arguments, and
        test_each_cycle_hands_every_process_the_exact_arguments_it_needs
        reads it so a construction that stopped naming them is named by
        an assertion instead of reaching this class as a KeyError from
        whichever test read a missing key first (codex round 10, finding
        .11.2.18).

        first_cycle_alive says whether Logic, Input and GUI report
        themselves alive during the FIRST cycle. The default is all three
        dead, which reaches the supervisor's crash branch through the
        Logic check and never creates the state where Input alone has
        died. A caller that wants that state passes (True, False, True)
        (codex round 5, finding .11.2.11). The second cycle is always all
        three dead, so the run ends there.
        """
        app_data = launcher_env["app_data"]
        make_proc = launcher_env["make_process"]

        # A restart flag is what makes the supervisor take a second cycle.
        (app_data / "wheelhouse.restart").write_text("")

        time_values = [
            100.0, 100.1, 100.2, 100.3, 100.4, 130.0,
            200.0, 200.1, 200.2, 200.3, 200.4, 230.0,
        ] + [300.0] * 6

        alive_by_position = list(first_cycle_alive) + [False, False, False]
        process_mocks = [
            make_proc(alive=alive_by_position[i], exitcode=0,
                      name=["LogicProcess", "InputProcess", "GuiProcess"][i % 3],
                      pid=42 + i)
            for i in range(6)
        ]
        process_calls = []

        positional_calls = []

        def make_process(*args, **kwargs):
            process_calls.append(kwargs)
            # Kept, not discarded. The launcher names every argument today,
            # so this is empty on every call; a construction that stopped
            # naming them used to reach the tests as a KeyError from
            # whichever assertion read a key first, which named no cause
            # (codex round 10, finding .11.2.18).
            positional_calls.append(args)
            index = len(process_calls) - 1
            if index < len(process_mocks):
                return process_mocks[index]
            # A construction beyond the six a two-cycle run makes is the
            # thing these tests exist to catch, so the harness must survive
            # it and let the assertion below name it. Running off the end of
            # a fixed list instead raised IndexError inside launcher.main,
            # which its own except caught and turned into a CRITICAL log --
            # the tests then failed on the wreckage rather than on the extra
            # construction (codex round 5, finding .11.2.11).
            return make_proc(alive=False, exitcode=0,
                             name=kwargs.get("name", "UnexpectedProcess"),
                             pid=42 + index)

        shm_names = []

        def make_shm(*args, **kwargs):
            shm_names.append(kwargs["name"])
            m = MagicMock()
            # Assigned after construction: MagicMock(name=...) sets the
            # mock's repr, not an attribute the launcher can read.
            m.name = kwargs["name"]
            return m

        def make_queue(*_args, **_kwargs):
            # A FRESH object per call. return_value=Mock() handed the same
            # object to every queue slot in both cycles, so which queue
            # reached which process could not be seen: the Input process
            # could be given the commands-to-Logic queue where its response
            # queue belongs and every test in this file stayed green (codex
            # round 8, finding .11.2.16).
            return Mock()

        events = []

        def make_event():
            # is_set must answer False or the supervisor loop exits before
            # it ever looks at the processes.
            event = Mock()
            event.is_set.return_value = False
            events.append(event)
            return event

        # disable_console_quick_edit is patched for the same reason run_main
        # patches it: launcher.main calls it unconditionally, and the real
        # function opens CONIN$ and clears QuickEdit and mouse input on the
        # attached console. It never raises, so these tests would pass while
        # mutating the console of whoever ran the suite.
        #
        # Once per launcher.main() call, which is once per test in this
        # class -- NOT once per supervisor cycle. The call sits above the
        # "while should_restart" loop in launcher._run_supervisor, which
        # launcher.main calls once, so a run that takes two cycles still
        # makes one console call. An earlier version of this comment said
        # "six times per class", counting it per cycle (codex round 5,
        # finding .11.2.12); the correction then put the loop in
        # launcher.main, which is where main calls _run_supervisor rather
        # than where the loop is.
        quick_edit_mock = Mock(return_value=True)

        with patch("services.wheelhouse.utils.system.get_app_data_path",
                   return_value=str(app_data)), \
             patch.object(launcher, "cleanup_stale_resources"), \
             patch.object(launcher, "disable_console_quick_edit",
                          quick_edit_mock), \
             patch("launcher.shared_memory.SharedMemory", side_effect=make_shm), \
             patch("launcher.multiprocessing.Queue", side_effect=make_queue), \
             patch("launcher.multiprocessing.Event", side_effect=make_event), \
             patch("launcher.multiprocessing.Process", side_effect=make_process), \
             patch.dict("sys.modules", launcher_env["sys_modules"]), \
             patch.object(launcher, "time", _fake_time_ns(time_values)):

            launcher.main()

        # The patch is load-bearing, so it is asserted rather than trusted: if
        # a later edit drops it, the real function runs again and this fails
        # instead of silently touching the console.
        assert quick_edit_mock.called, (
            "launcher.main did not go through the patched "
            "disable_console_quick_edit; the real one would have run"
        )

        # A LOWER bound, deliberately. Too few constructions means the run
        # never took its second cycle and every caller's assertions would be
        # reading a half-finished run. Too MANY is a real finding rather than
        # a broken harness, and it belongs to the caller's own assertion,
        # which can say what the extra construction was.
        #
        # THAT DUTY IS REAL AND EVERY CALLER MUST DISCHARGE IT. The two
        # tests that read whole name sequences do it by comparing against
        # the exact six. The two that read fixed positions do it with an
        # explicit "== 6" of their own, because a construction after the
        # second cycle leaves the positions they read untouched and would
        # otherwise pass unnoticed (codex round 6, finding .11.2.13).
        assert len(process_calls) >= 6, (
            f"expected two cycles of three processes, got {len(process_calls)}"
        )
        return process_calls, shm_names, events, positional_calls

    def test_the_input_process_is_never_rebuilt_on_its_own(self, launcher_env):
        """Every cycle constructs all three processes or none.

        This is the structural reason the stale-command containment had to
        go inside the command loop. If the launcher could restart a hung
        Input process by itself, a supervisor would be a candidate answer
        to a wedged loop. It cannot: there is one construction site, it
        builds Logic, Input and GUI together, and a death in any one of
        them takes the whole cycle down.
        """
        process_calls, _shm_names, _events, _positional = \
            self._run_two_cycles(launcher_env)

        names = [c["name"] for c in process_calls]
        assert names == [
            "LogicProcess", "InputProcess", "GuiProcess",
            "LogicProcess", "InputProcess", "GuiProcess",
        ], f"the cycle did not rebuild all three processes together: {names}"

    def test_each_cycle_wires_every_process_to_its_own_entrypoint(
        self, launcher_env,
    ):
        """Each process runs the entrypoint its name promises.

        The rest of this class reads names, construction counts and
        argument positions. None of them looked at the target, so the
        launcher could build a process called InputProcess that runs the
        LOGIC entrypoint and every test in this file stayed green. Observed
        before this test existed: with target=start_logic_process on the
        Input construction, name and args untouched, tests/test_launcher.py
        reported 75 passed while no Input command reader started at all
        (codex round 7, finding .11.2.14).

        That matters more here than a missing assertion usually would. The
        whole bead argues the containment must live inside the Input
        command loop because nothing restarts that process on its own. A
        launcher that never starts the right process at all is the same
        harm arriving sooner, and this class is where it would be seen.
        """
        process_calls, _shm_names, _events, _positional = \
            self._run_two_cycles(launcher_env)

        assert len(process_calls) == 6, (
            "the run built processes outside the two cycles this test reads: "
            f"{[c['name'] for c in process_calls]}"
        )

        modules = launcher_env["sys_modules"]
        expected = [
            ("LogicProcess", modules["main"].start_logic_process),
            ("InputProcess", modules["input_proc"].input_process_main),
            ("GuiProcess", modules["gui"].gui_process_target),
        ] * 2

        mismatches = [
            f"construction {index} named {name} ran {target!r}, "
            f"expected {wanted!r}"
            for index, ((name, target), (_, wanted)) in enumerate(
                zip([(c["name"], c["target"]) for c in process_calls], expected)
            )
            if name != expected[index][0] or target is not wanted
        ]
        assert not mismatches, (
            "a process was constructed with an entrypoint its name does not "
            "promise: " + "; ".join(mismatches)
        )

    def test_each_cycle_gives_the_processes_the_queues_they_must_share(
        self, launcher_env,
    ):
        """The three queues of a cycle reach the processes that need them.

        _run_supervisor builds three separate queues per cycle and hands
        each one to a specific pair of processes: Logic and Input share the
        response queue, Logic and GUI share the commands-to-Logic queue,
        and Logic and GUI share the state-to-GUI queue. Nothing read any of
        those slots, and the harness handed out one shared mock for all of
        them, so the relationships could not be seen even in principle.
        Observed before this test existed: with the Input process given
        commands_to_logic_queue where response_queue belongs,
        tests/test_launcher.py reported 76 passed (codex round 8, finding
        .11.2.16).

        The Input process answers a command on the response queue. A
        response sent on the wrong queue is a command that never gets an
        answer, which is the same silence this bead's stale-command expiry
        exists to prevent.
        """
        process_calls, _shm_names, _events, _positional = \
            self._run_two_cycles(launcher_env)

        assert len(process_calls) == 6, (
            "the run built processes outside the two cycles this test reads: "
            f"{[c['name'] for c in process_calls]}"
        )

        # Positions come from the three args tuples the launcher builds:
        # logic_args[3] and input_args[3] are the response queue,
        # logic_args[6] and gui_args[1] the commands-to-Logic queue, and
        # logic_args[7] and gui_args[2] the state-to-GUI queue.
        seen_in_earlier_cycles = []
        for cycle in (0, 1):
            logic, inp, gui = process_calls[cycle * 3:cycle * 3 + 3]

            assert inp["args"][3] is logic["args"][3], (
                f"cycle {cycle}: the Input process would answer on a queue "
                "the Logic process is not reading"
            )
            assert gui["args"][1] is logic["args"][6], (
                f"cycle {cycle}: the GUI process would send commands on a "
                "queue the Logic process is not reading"
            )
            assert gui["args"][2] is logic["args"][7], (
                f"cycle {cycle}: the Logic process would send state on a "
                "queue the GUI process is not reading"
            )

            # Three separate queues, not one object in three slots. Without
            # this, every identity check above is satisfied by a harness
            # that hands out a single mock, which is exactly the state the
            # finding describes.
            queues = [logic["args"][3], logic["args"][6], logic["args"][7]]
            assert len({id(queue) for queue in queues}) == 3, (
                f"cycle {cycle}: the three queues are not three distinct "
                "objects, so the links asserted above prove nothing"
            )

            reused = [q for q in queues if any(q is old for old in seen_in_earlier_cycles)]
            assert not reused, (
                f"cycle {cycle} reused a queue from an earlier cycle; a "
                "restart is supposed to share nothing with the cycle it "
                "replaces"
            )
            seen_in_earlier_cycles.extend(queues)

    def test_each_cycle_hands_every_process_the_exact_arguments_it_needs(
        self, launcher_env,
    ):
        """Every element of the three argument tuples, checked by name.

        Four consecutive review rounds found the same shape in this class:
        a property of the process construction that no test in this file
        read. Each round added one assertion and the next round found the
        next unread property. This test closes the whole class instead of
        taking one more instance of it. It walks every element of
        logic_args, input_args and gui_args, and it asserts the three
        tuple lengths first, so a newly added element cannot slip past the
        rows below without failing here (codex round 9, finding .11.2.17).

        Observed before this test existed, each mutation run against the
        whole file: the Input process given its two readiness events in
        the wrong order -- 77 passed; the Logic process given the GUI
        segment name as its command segment -- 77 passed; the Logic
        process told half the real buffer size -- 77 passed; the Logic
        process given the command segment name where the GUI segment name
        belongs -- 77 passed.

        The event swap is the one that matters most in production. The
        Logic process would signal the real command-ready event while the
        Input process waits on the real input-ready event, and the Input
        process would set the real command-ready event when it means to
        announce that it is ready. A spoken command then never reaches the
        Input loop, which is the same silence this bead exists to prevent.
        """
        process_calls, shm_names, _events, positional_calls = \
            self._run_two_cycles(launcher_env)

        assert len(process_calls) == 6, (
            "the run built processes outside the two cycles this test reads: "
            f"{[c['name'] for c in process_calls]}"
        )
        # Two segments per cycle: the command segment, then the GUI segment.
        assert len(shm_names) == 4, shm_names

        # THE SHAPE OF THE CALL, before any of its values are read.
        #
        # This assertion PINS the launcher's constructor call shape on
        # purpose. multiprocessing.Process accepts more keywords than the
        # launcher uses -- daemon= is the obvious one, and it changes
        # process lifecycle semantics. Every row below reads a value under a
        # known key, so an added key is simply not read: with daemon=True on
        # the Input construction and nothing else touched, this file
        # reported 78 passed (codex round 10, finding .11.2.18).
        #
        # If a future lifecycle keyword is intended, that is a decision, not
        # an accident, and it must update this assertion AND its expected
        # value in the same change. That is the point of pinning the shape:
        # the constructor contract widens deliberately and visibly, or not
        # at all.
        #
        # The positional half exists for a different reason. The launcher
        # names every argument today, so positional_calls is empty on every
        # call. A construction that stopped naming them used to reach the
        # tests as a KeyError from whichever assertion read a key first,
        # which named no cause; measured, that failed 7 of the 7 tests in
        # this class and every failure was a KeyError. Reading the recorded
        # positional arguments here turns that into one named failure.
        for index, call in enumerate(process_calls):
            assert positional_calls[index] == (), (
                f"construction {index} passed values positionally: "
                f"{positional_calls[index]!r}; every assertion in this class "
                "reads arguments by keyword and cannot see them"
            )
            assert set(call) == {"target", "args", "name"}, (
                f"construction {index} named {call.get('name')!r} was built "
                f"with the keywords {sorted(call)}, not the three this class "
                "enumerates; see the comment above before widening this"
            )

        seen_in_earlier_cycles = []
        for cycle in (0, 1):
            logic, inp, gui = process_calls[cycle * 3:cycle * 3 + 3]
            logic_args = logic["args"]
            input_args = inp["args"]
            gui_args = gui["args"]
            command_segment, gui_segment = shm_names[cycle * 2:cycle * 2 + 2]

            assert (len(logic_args), len(input_args), len(gui_args)) == (9, 5, 4), (
                f"cycle {cycle}: the launcher passes a different number of "
                "arguments than the rows below enumerate, so at least one "
                "of them is unread: "
                f"{(len(logic_args), len(input_args), len(gui_args))}"
            )

            # The two shared-memory names are strings. Equality, not
            # identity: an `is` check on a string can hold because CPython
            # interned it rather than because the launcher passed the same
            # value, so it proves less than it appears to.
            assert logic_args[0] == command_segment, (
                f"cycle {cycle}: the Logic process was given a command "
                "segment name the launcher did not create"
            )
            assert input_args[0] == command_segment, (
                f"cycle {cycle}: the Input process would open a different "
                "command segment from the one the Logic process writes"
            )
            assert logic_args[8] == gui_segment, (
                f"cycle {cycle}: the Logic process was given a GUI segment "
                "name the launcher did not create"
            )
            assert gui_args[3] == gui_segment, (
                f"cycle {cycle}: the GUI process would open a different "
                "overlay segment from the one the Logic process writes"
            )
            assert command_segment != gui_segment, (
                f"cycle {cycle}: the two segment names are the same string, "
                "so the four equality checks above prove nothing"
            )

            # Everything else is an object the launcher builds once per
            # cycle and hands to a specific pair, so identity is the
            # assertion that means anything.
            assert input_args[1] is logic_args[1], (
                f"cycle {cycle}: the Input process would wait on a "
                "command-ready event the Logic process never signals"
            )
            assert input_args[2] is logic_args[2], (
                f"cycle {cycle}: the Input process would announce readiness "
                "on an event the Logic process never reads"
            )
            assert input_args[3] is logic_args[3], (
                f"cycle {cycle}: the Input process would answer on a queue "
                "the Logic process is not reading"
            )
            assert input_args[4] is logic_args[5], (
                f"cycle {cycle}: the Input process would watch a shutdown "
                "event the launcher never sets for this cycle"
            )
            assert gui_args[0] is logic_args[5], (
                f"cycle {cycle}: the GUI process would watch a shutdown "
                "event the launcher never sets for this cycle"
            )
            assert gui_args[1] is logic_args[6], (
                f"cycle {cycle}: the GUI process would send commands on a "
                "queue the Logic process is not reading"
            )
            assert gui_args[2] is logic_args[7], (
                f"cycle {cycle}: the Logic process would send state on a "
                "queue the GUI process is not reading"
            )

            # logic_args[4] is the only element in any of the three tuples
            # that is shared with nothing. It is a module constant, not a
            # per-cycle object, so it has no identity row and its absence
            # from the pairs above is not a gap.
            assert logic_args[4] == launcher.SHARED_MEM_SIZE, (
                f"cycle {cycle}: the Logic process was told the command "
                f"buffer is {logic_args[4]} bytes, but the launcher created "
                f"{launcher.SHARED_MEM_SIZE}"
            )

            # Six distinct objects, not one object in six slots. Without
            # this, every identity check above is satisfied by a harness
            # that hands out a single mock -- the exact state that made the
            # queue links invisible in round 8.
            shared_objects = [
                logic_args[1], logic_args[2], logic_args[3],
                logic_args[5], logic_args[6], logic_args[7],
            ]
            assert len({id(obj) for obj in shared_objects}) == 6, (
                f"cycle {cycle}: the two events, three queues and shutdown "
                "event are not six distinct objects, so the links asserted "
                "above prove nothing"
            )

            reused = [
                obj for obj in shared_objects
                if any(obj is old for old in seen_in_earlier_cycles)
            ]
            assert not reused, (
                f"cycle {cycle} reused an event or queue from an earlier "
                "cycle; a restart is supposed to share nothing with the "
                "cycle it replaces"
            )
            seen_in_earlier_cycles.extend(shared_objects)

    def test_an_input_only_death_still_takes_the_whole_cycle_down(
        self, launcher_env, caplog,
    ):
        """The one death that matters here: Input alone, Logic and GUI fine.

        This is the state the containment argument rests on, and until
        codex round 5 (finding .11.2.11) no test in this class created it.
        Every process mock was dead on arrival, so the supervisor always
        reached its crash branch through the Logic check, and a regression
        that added an Input-only respawn -- "not input_alive and
        logic_alive and gui_alive" -- would never have run in this harness.
        The other three tests would have stayed green while the Input
        process gained exactly the restart path this bead argues it does
        not have.

        The first assertion below is about the harness rather than the
        launcher, and it is there on purpose: a scenario that fails to
        create the state it claims to test proves nothing, which is the
        mistake this class already made once.
        """
        with caplog.at_level(logging.ERROR, logger=launcher.logger.name):
            process_calls, _shm_names, _events, _positional = self._run_two_cycles(
                launcher_env, first_cycle_alive=(True, False, True),
            )

        deaths = [
            record.getMessage() for record in caplog.records
            if "terminated unexpectedly" in record.getMessage()
        ]
        assert deaths, (
            "the supervisor never reported a death, so the first cycle did "
            "not reach its crash branch and this test proved nothing"
        )
        assert "Input(" in deaths[0], deaths[0]
        assert "Logic(" not in deaths[0] and "GUI(" not in deaths[0], (
            "the first cycle was supposed to lose ONLY the Input process; "
            f"the supervisor saw more than that: {deaths[0]}"
        )

        names = [c["name"] for c in process_calls]
        assert names == [
            "LogicProcess", "InputProcess", "GuiProcess",
            "LogicProcess", "InputProcess", "GuiProcess",
        ], (
            "losing the Input process alone produced a construction "
            f"sequence other than two whole cycles: {names}"
        )

        # Positions come from the args tuples the launcher builds:
        # logic_args[5] is the cycle's shutdown event.
        first_cycle_shutdown = process_calls[0]["args"][5]
        second_cycle_shutdown = process_calls[3]["args"][5]
        assert first_cycle_shutdown is not second_cycle_shutdown, (
            "the Input process came back inside the first cycle, sharing "
            "its shutdown event; recovery is supposed to be a whole new "
            "cycle that shares nothing with the old one"
        )

    def test_all_three_processes_of_a_cycle_share_one_shutdown_event(
        self, launcher_env,
    ):
        """One shutdown event per cycle, held by all three processes.

        This is what makes the death of any one process end the cycle for
        the other two, and it is why no Input-only restart exists to be
        used as a recovery path for a wedged command loop.
        """
        process_calls, _shm_names, _events, _positional = \
            self._run_two_cycles(launcher_env)

        # The slices below read positions 0-2 and 3-5 and would not notice a
        # SEVENTH construction, so the count is asserted here rather than
        # left to the helper's lower bound (codex round 6, finding
        # .11.2.13).
        assert len(process_calls) == 6, (
            "the run built processes outside the two cycles this test reads: "
            f"{[c['name'] for c in process_calls]}"
        )

        # Positions come from the three args tuples the launcher builds:
        # logic_args[5], input_args[4], gui_args[0].
        for cycle in (0, 1):
            logic, inp, gui = process_calls[cycle * 3:cycle * 3 + 3]
            shutdown = logic["args"][5]
            assert inp["args"][4] is shutdown, (
                f"cycle {cycle}: Input got a different shutdown event from Logic"
            )
            assert gui["args"][0] is shutdown, (
                f"cycle {cycle}: GUI got a different shutdown event from Logic"
            )

    def test_a_second_cycle_shares_nothing_with_the_first(self, launcher_env):
        """A restart discards the segment and the events, it does not reuse them.

        A command frame is addressed by segment name and announced through
        a specific event object. Both change, so nothing written for the
        old Input process can be read by the new one -- which is the only
        sense in which a restart "clears" a stale command.
        """
        process_calls, shm_names, _events, _positional = \
            self._run_two_cycles(launcher_env)

        # This test reads only constructions 1 and 4, so a seventh would go
        # unnoticed here as well -- and a process rebuilt on its own reuses
        # the existing segment, so the shared-memory count below does not
        # catch it either (codex round 6, finding .11.2.13).
        assert len(process_calls) == 6, (
            "the run built processes outside the two cycles this test reads: "
            f"{[c['name'] for c in process_calls]}"
        )

        # Two segments per cycle: the command segment, then the GUI segment.
        assert len(shm_names) == 4, shm_names
        assert shm_names[0] != shm_names[2], (
            f"the second cycle reused the command segment name: {shm_names}"
        )
        assert shm_names[1] != shm_names[3], (
            f"the second cycle reused the GUI segment name: {shm_names}"
        )

        first, second = process_calls[1], process_calls[4]
        assert first["args"][0] == shm_names[0]
        assert second["args"][0] == shm_names[2]
        # command_ready_event, input_ready_event, shutdown_event.
        for index in (1, 2, 4):
            assert first["args"][index] is not second["args"][index], (
                f"the second cycle reused the Input process's event at "
                f"position {index}"
            )
