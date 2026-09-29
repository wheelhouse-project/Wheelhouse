"""A native crash in a provider leaves a trace on disk.

wh-provider-native-crash-trace, stage 1.

The defect. On MavenCore under public 1.0.8 the Parakeet provider died
three times with exit code 3221225477 (0xC0000005, an access violation
inside native code). The log held only the exit code, and only in the
launcher's console window: run_launcher() started main.py with no
stderr and no faulthandler, so the Python stack of the crashing thread
was never written anywhere, and nothing reached wheelhouse.log.

The remedy on this side of the boundary: the supervisor starts main.py
with PYTHONFAULTHANDLER=1, so the interpreter enables faulthandler
before its first import (an import-time DLL crash is the case
runtime_dll_directory.py records); it sends the child's stderr to a
file in the WheelHouse application-data folder, never to a pipe, so a
full buffer can never stop the provider; and after each child exits it
appends one line naming the exit code, so WheelHouse can read the code
for the first time.

The tests below run real child processes. The first one crashes a real
interpreter with an access violation: a simulated crash would not prove
that the interpreter writes its dump into the file.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from shared_stt import launcher as launcher_module
from shared_stt.launcher import LauncherConfig, run_launcher

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the crash code and paths are Windows ones")


def _run(tmp_path, monkeypatch, script_text, *, app_name="CrashProbe",
         max_crashes=1):
    """Run the shipped supervisor loop over a real child script.

    Only the signal handlers and the root logging setup are stubbed:
    both change process-wide state in the pytest process. APPDATA points
    at a temporary folder so nothing lands in the real one.
    """
    launcher_dir = tmp_path / "provider"
    launcher_dir.mkdir(exist_ok=True)
    script = launcher_dir / "child.py"
    script.write_text(script_text, encoding="utf-8")
    appdata = tmp_path / "appdata"
    appdata.mkdir(exist_ok=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("PYTHONFAULTHANDLER", raising=False)

    config = LauncherConfig(
        app_name=app_name,
        main_script="child.py",
        launcher_dir=str(launcher_dir),
        max_crashes=max_crashes,
    )
    with patch("shared_stt.launcher.signal.signal"), \
         patch("shared_stt.launcher.logging.basicConfig"):
        run_launcher(config)

    return script, appdata / "WheelHouse" / f"{app_name.lower()}.stderr.log"


class TestARealNativeCrash:

    def test_the_crash_dump_and_the_exit_code_reach_the_file(
            self, tmp_path, monkeypatch):
        """The child starts a native thread at address 0. That thread
        faults at once with STATUS_ACCESS_VIOLATION -- the code MavenCore
        recorded -- on a thread no Python code runs on, which is how a
        native library's worker thread dies.

        Two simpler crashes do not work. ctypes.string_at(0) is caught:
        ctypes wraps every foreign call in a structured exception
        handler and turns the fault into a Python OSError, exit code 1.
        faulthandler._sigsegv() raises the SIGSEGV signal on Windows
        instead, exit code 3.

        The child never enables faulthandler, so the dump can only come
        from the environment the supervisor gave it. The main thread is
        asleep on line 5 when the fault arrives; faulthandler lists it."""
        script, log_path = _run(
            tmp_path, monkeypatch,
            "import ctypes, time\n"
            "create_thread = ctypes.windll.kernel32.CreateThread\n"
            "create_thread.restype = ctypes.c_void_p\n"
            "create_thread(None, 0, None, None, 0, None)\n"
            "time.sleep(30)\n",
        )

        text = log_path.read_bytes().decode("utf-8", "backslashreplace")
        assert "Windows fatal exception: access violation" in text, text
        assert f'File "{script}", line 5' in text, text
        assert ("[launcher] CrashProbe process exited with code "
                "3221225477 (0xC0000005)") in text, text

    def test_the_child_gets_the_faulthandler_setting(
            self, tmp_path, monkeypatch):
        """The control for the test above, and a direct reading of the
        variable, so a mutant that sets it to the wrong value fails
        here rather than only through a crash."""
        _, log_path = _run(
            tmp_path, monkeypatch,
            "import os, sys\n"
            "sys.stderr.write('FH=' + repr(os.environ.get('PYTHONFAULTHANDLER')))\n",
        )

        assert "FH='1'" in log_path.read_text(encoding="utf-8")


class TestTheCaptureCannotBlock:

    def test_a_child_that_writes_more_than_a_pipe_holds_finishes(
            self, tmp_path, monkeypatch):
        """A Windows pipe buffer is a few kilobytes. With a pipe that
        nobody reads, the child below would stop inside its write and
        the supervisor would wait on it for ever. A file has no such
        limit."""
        done = threading.Event()
        result = {}

        def body():
            try:
                result["paths"] = _run(
                    tmp_path, monkeypatch,
                    "import sys\n"
                    "sys.stderr.write('x' * 200000)\n"
                    "sys.stderr.flush()\n",
                )
            finally:
                done.set()

        worker = threading.Thread(target=body, daemon=True)
        worker.start()
        assert done.wait(60), "the supervisor did not return within 60 s"
        _, log_path = result["paths"]
        assert log_path.stat().st_size >= 200000


class TestTheExitLine:

    def test_a_clean_exit_is_recorded_with_its_code(
            self, tmp_path, monkeypatch):
        _, log_path = _run(tmp_path, monkeypatch, "pass\n")

        lines = log_path.read_text(encoding="utf-8").splitlines()
        exit_lines = [line for line in lines if line.startswith("[launcher] ")]
        assert len(exit_lines) == 1, lines
        assert exit_lines[0].startswith(
            "[launcher] CrashProbe process exited with code 0 (0x00000000) after "
        ), exit_lines[0]
        assert exit_lines[0].endswith("s"), exit_lines[0]

    def test_every_attempt_gets_its_own_line(self, tmp_path, monkeypatch):
        """A fast crash is restarted up to max_crashes times. WheelHouse
        reads the last line, and a person reading the file sees each
        attempt."""
        _, log_path = _run(
            tmp_path, monkeypatch, "raise SystemExit(7)\n", max_crashes=2)

        exit_lines = [
            line for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("[launcher] ")
        ]
        assert len(exit_lines) == 2, exit_lines
        assert all("code 7 (0x00000007)" in line for line in exit_lines)

    def test_a_previous_run_is_kept(self, tmp_path, monkeypatch):
        """The file is opened for append: the trace of the crash before a
        restart must survive the restart."""
        appdata = tmp_path / "appdata" / "WheelHouse"
        appdata.mkdir(parents=True)
        (appdata / "crashprobe.stderr.log").write_bytes(b"EARLIER RUN\n")

        _, log_path = _run(tmp_path, monkeypatch, "pass\n")

        assert log_path.read_bytes().startswith(b"EARLIER RUN\n")


class TestTheSizeBound:

    def test_a_large_file_is_moved_aside_at_child_start(
            self, tmp_path, monkeypatch):
        appdata = tmp_path / "appdata" / "WheelHouse"
        appdata.mkdir(parents=True)
        old = appdata / "crashprobe.stderr.log"
        old.write_bytes(b"o" * (launcher_module.STDERR_LOG_MAX_BYTES + 1))

        _, log_path = _run(tmp_path, monkeypatch, "pass\n")

        moved = appdata / "crashprobe.stderr.log.1"
        assert moved.stat().st_size == launcher_module.STDERR_LOG_MAX_BYTES + 1
        assert log_path.stat().st_size < 1000

    def test_a_file_at_the_bound_is_not_moved(self, tmp_path, monkeypatch):
        appdata = tmp_path / "appdata" / "WheelHouse"
        appdata.mkdir(parents=True)
        (appdata / "crashprobe.stderr.log").write_bytes(
            b"o" * launcher_module.STDERR_LOG_MAX_BYTES)

        _run(tmp_path, monkeypatch, "pass\n")

        assert not (appdata / "crashprobe.stderr.log.1").exists()

    def test_a_file_that_cannot_be_moved_is_appended_to(
            self, tmp_path, monkeypatch):
        """A provider left behind by an earlier WheelHouse still holds
        the file as its stderr, and Windows then refuses the rename. The
        new child must still start and still write its trace."""
        appdata = tmp_path / "appdata" / "WheelHouse"
        appdata.mkdir(parents=True)
        big = b"o" * (launcher_module.STDERR_LOG_MAX_BYTES + 1)
        (appdata / "crashprobe.stderr.log").write_bytes(big)

        with patch("shared_stt.launcher.os.replace",
                   side_effect=PermissionError("in use")):
            _, log_path = _run(
                tmp_path, monkeypatch,
                "import sys\nsys.stderr.write('CHILD-TRACE')\n")

        data = log_path.read_bytes()
        assert data.startswith(big)
        assert b"CHILD-TRACE" in data
        assert b"[launcher] CrashProbe process exited with code 0" in data


class TestAnUnwritableFile:

    def test_the_provider_still_starts(self, tmp_path, monkeypatch):
        """The trace is a diagnostic. A folder that refuses the file must
        not cost the person their speech recognition."""
        marker = tmp_path / "child-ran"
        with patch("shared_stt.launcher._open_stderr_log",
                   side_effect=PermissionError("denied")):
            _run(tmp_path, monkeypatch,
                 f"open({str(marker)!r}, 'w').close()\n")

        assert marker.exists(), "the provider did not start"


def test_the_path_is_in_the_application_data_folder(monkeypatch, tmp_path):
    """WheelHouse reads this file from its own app_data_dir, which is
    %APPDATA%\\WheelHouse; remote_stt_launcher.py builds the same name.
    The name is lower case, like the pid file next to it."""
    monkeypatch.setenv("APPDATA", str(tmp_path))

    path = launcher_module.get_stderr_log_path("Parakeet_TDT")

    # Compared as strings: WindowsPath equality ignores case.
    assert os.path.basename(path) == "parakeet_tdt.stderr.log"
    assert Path(path).parent == tmp_path / "WheelHouse"
    assert os.path.isdir(tmp_path / "WheelHouse")
