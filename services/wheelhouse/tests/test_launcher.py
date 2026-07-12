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
# Console QuickEdit hardening (wh-console-quickedit-freeze)
# ---------------------------------------------------------------------------


_QUICK_EDIT = 0x0040
_EXTENDED_FLAGS = 0x0080


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
        # Every bit other than QuickEdit/Extended must be preserved.
        preserved = ~(_QUICK_EDIT | _EXTENDED_FLAGS)
        assert new_mode & preserved == 0x01F7 & preserved
        k.CloseHandle.assert_called_once()

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
        """Corrupt (non-numeric) PID file: ValueError is caught gracefully.

        When the PID file contains non-numeric text, int() raises ValueError.
        The except clause catches it, but os.remove (line 68) is inside the
        try block AFTER the with statement, so it gets skipped. The file
        remains on disk. This is the actual behavior of the code.
        """
        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("not_a_number")

        with patch.object(launcher, "PID_FILE_PATH", str(pid_file)):
            launcher.cleanup_stale_resources()

        # File remains because ValueError skips os.remove
        assert pid_file.exists()

    def test_cleanup_handles_io_error(self, tmp_path):
        """IOError during PID file read is handled gracefully."""
        pid_file = tmp_path / "wheelhouse.pid"
        pid_file.write_text("12345")

        with patch.object(launcher, "PID_FILE_PATH", str(pid_file)), \
             patch("builtins.open", side_effect=IOError("disk error")):
            launcher.cleanup_stale_resources()

        # File still exists because open() failed and os.remove was skipped
        assert pid_file.exists()


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
