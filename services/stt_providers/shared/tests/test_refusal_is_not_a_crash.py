"""A provider that refuses to start is not restarted by its supervisor.

wh-capture-winrt-required.1.5.

The defect. Every refusal site in every provider exited 1: the A3
constructor refusal (the capture factory refuses when winsdk is
missing) and the A9 capture refusal (the capture builds and then never
opens). A normal WheelHouse launch does not run main.py directly -- it
runs the provider's launcher.py, which is run_launcher() in
shared_stt/launcher.py, and that supervisor reads should_restart(). For
any nonzero exit inside crash_threshold_s (15.0 s by default) that
function answered True, so a refusal reached on a fast machine -- a
model already in the file cache, then a missing device -- started the
child again, up to max_crashes (3). WheelHouse had already consumed the
first startup_failed and reported the launch stopped.

The remedy is one exit code that means "refused", shared by the
providers that produce it and the supervisor that reads it.

Why the tests below spell the number rather than importing it. The exit
code crosses a process boundary: the provider writes it into its own
process exit status and the supervisor reads it back off a returncode.
A test that read the constant on both sides would keep passing if the
value moved onto a code some provider already uses -- google's
config_loader exits 2, and 1 is every other failure in the tree. The
literal is the wire value, and one test below ties the constant to it.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from shared_stt.launcher import LauncherConfig, run_launcher, should_restart

# The wire value. See the module docstring for why this is not imported.
REFUSAL_EXIT_STATUS = 3


class TestTheDecision:
    """should_restart is the whole of the supervisor's restart policy."""

    def test_a_refusal_is_not_restarted_even_on_a_fast_machine(self):
        """The uptime here is the one that made the defect reachable: a
        cached model load followed by a device that will not open,
        finished well inside the fifteen-second crash window."""
        result = should_restart(
            exit_code=REFUSAL_EXIT_STATUS,
            uptime=2.8,
            restart_flag_exists=False,
            crash_threshold_s=15.0,
        )

        assert result is False, (
            "a provider that refused to start was scheduled for another "
            "attempt")

    def test_the_refusal_is_reported_as_a_refusal(self, caplog):
        """The launcher log is where a person looks after a provider
        fails to appear. Without this line the log shows a child that
        exited and a supervisor that quit, with nothing saying the
        provider itself declined."""
        with caplog.at_level(logging.INFO, logger="shared_stt.launcher"):
            should_restart(
                exit_code=REFUSAL_EXIT_STATUS,
                uptime=2.8,
                restart_flag_exists=False,
                crash_threshold_s=15.0,
            )

        refusal_lines = [
            record.getMessage() for record in caplog.records
            if "refused to start" in record.getMessage()
        ]
        assert len(refusal_lines) == 1, (
            f"expected one refusal line, got {refusal_lines}")
        assert "not be restarted" in refusal_lines[0]

    def test_an_ordinary_crash_is_still_restarted(self):
        """The control. Crash recovery is what the supervisor is for,
        and the refusal code must not have taken it away."""
        result = should_restart(
            exit_code=1,
            uptime=2.8,
            restart_flag_exists=False,
            crash_threshold_s=15.0,
        )

        assert result is True

    def test_an_intentional_restart_still_wins_over_a_refusal(self):
        """WheelHouse asks for a restart by writing the flag file. That
        request is a deliberate act by the running system, so it
        outranks the previous child's opinion of itself -- the same
        precedence the flag already had over exit code 0."""
        result = should_restart(
            exit_code=REFUSAL_EXIT_STATUS,
            uptime=2.8,
            restart_flag_exists=True,
            crash_threshold_s=15.0,
        )

        assert result is True


def _run_launcher_against_a_child_that_exits(tmp_path, exit_code):
    """Run the real supervisor loop over a child that exits at once.

    Returns the list of commands the supervisor started, so the count is
    the number of children it ran. Only the process boundary and the
    signal handlers are stubbed: the decision, the crash counting and
    the loop are the shipped ones.
    """
    started = []

    class FakePopen:
        def __init__(self, cmd, cwd=None):
            started.append(cmd)
            self.pid = 4242
            self.returncode = None

        def wait(self):
            self.returncode = exit_code

        def poll(self):
            return exit_code

    config = LauncherConfig(
        app_name="RefusalProbe",
        main_script="main.py",
        launcher_dir=str(tmp_path),
    )

    with patch("shared_stt.launcher.subprocess.Popen", FakePopen), \
         patch("shared_stt.launcher.signal.signal"), \
         patch("shared_stt.launcher.logging.basicConfig"), \
         patch("shared_stt.launcher.get_pid_file_path",
               return_value=str(tmp_path / "refusalprobe.pid")), \
         patch("shared_stt.launcher.get_restart_flag_path",
               return_value=str(tmp_path / "refusalprobe.restart")):
        run_launcher(config)

    return started


class TestTheSupervisorLoop:
    """The count of children is the harm the person actually sees."""

    def test_a_refusal_runs_one_child_and_stops(self, tmp_path):
        """max_crashes is 3, so the unfixed supervisor ran the refusing
        provider three times -- three model loads, three refusals, and
        three notices against a launch WheelHouse had already recorded
        as stopped."""
        started = _run_launcher_against_a_child_that_exits(
            tmp_path, REFUSAL_EXIT_STATUS)

        assert len(started) == 1, (
            f"the supervisor started {len(started)} children for a "
            "provider that refused to start")

    def test_a_refusal_is_not_counted_toward_the_crash_limit(
            self, tmp_path, caplog):
        """A refusal that consumed a crash slot would leave a later real
        crash with fewer attempts than the configuration promises, and
        it would end the run with a "crashed too many times" line that
        names the wrong cause."""
        with caplog.at_level(logging.INFO, logger="shared_stt.launcher"):
            _run_launcher_against_a_child_that_exits(
                tmp_path, REFUSAL_EXIT_STATUS)

        crash_lines = [
            record.getMessage() for record in caplog.records
            if "Crash count" in record.getMessage()
            or "crashed too many times" in record.getMessage()
        ]
        assert crash_lines == [], (
            f"the refusal was counted as a crash: {crash_lines}")

    def test_an_ordinary_crash_still_uses_every_attempt(self, tmp_path):
        """The control for the loop. A child that really crashes still
        gets max_crashes attempts."""
        started = _run_launcher_against_a_child_that_exits(tmp_path, 1)

        assert len(started) == 3


def test_the_constant_carries_the_wire_value():
    """The providers import this name; the supervisor compares against
    the number that came back from the operating system. This is the one
    place the two are tied together.

    Not 0: a clean stop in the launcher log, and to a person running the
    provider by hand. Not 1: every other failure in the tree already
    uses it, so a supervisor that read 1 as a refusal would stop
    restarting real crashes.
    """
    from shared_stt.startup_refusal import REFUSAL_EXIT_CODE

    assert REFUSAL_EXIT_CODE == REFUSAL_EXIT_STATUS
    assert REFUSAL_EXIT_CODE not in (0, 1)
