"""Tests for launcher supervision of the console-probe helper subprocess.

The helper that owns all AttachConsole calls runs as a ``subprocess.Popen``
child (NOT a ``multiprocessing.Process``) so it can own its own console
attachment in isolation. The launcher supervises it: spawns it, watches it in
the alive-check loop, and restarts it on crash following the same crash-count
discipline as the trio of Logic/Input/GUI processes.

These tests exercise the testable seams the launcher exposes for the helper so
the supervision contract is asserted without spawning real processes:

  * the helper spawn command targets ``console_probe_helper`` and discards
    stderr at the OS level (``stderr=DEVNULL``) so the helper can never leak
    text to a foreign terminal;
  * a liveness predicate reports the helper dead when ``poll()`` is not None;
  * a restart decision honours a maximum restart budget then degrades safely.
"""

import subprocess

import launcher


class TestHelperSpawnCommand:
    def test_spawn_uses_console_probe_helper_module(self):
        cmd = launcher._console_probe_helper_command()
        joined = " ".join(cmd)
        # The command must launch the helper entry point.
        assert "console_probe_helper" in joined

    def test_spawn_redirects_stderr_to_devnull(self):
        captured = {}

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return object()

        launcher._spawn_console_probe_helper(popen=_fake_popen)

        assert captured["kwargs"].get("stderr") == subprocess.DEVNULL
        # stdin/stdout must be pipes so the client can talk to it.
        assert captured["kwargs"].get("stdin") == subprocess.PIPE
        assert captured["kwargs"].get("stdout") == subprocess.PIPE


class TestHelperLiveness:
    def test_alive_when_poll_none(self):
        class _Proc:
            def poll(self):
                return None

        assert launcher._console_probe_helper_alive(_Proc()) is True

    def test_dead_when_poll_returns_exit_code(self):
        class _Proc:
            def poll(self):
                return 1

        assert launcher._console_probe_helper_alive(_Proc()) is False

    def test_dead_when_none(self):
        assert launcher._console_probe_helper_alive(None) is False


class TestHelperRestartBudget:
    def test_restart_allowed_under_budget(self):
        assert launcher._should_restart_console_probe_helper(0) is True
        assert launcher._should_restart_console_probe_helper(
            launcher.MAX_CONSOLE_PROBE_RESTARTS - 1
        ) is True

    def test_restart_denied_at_budget(self):
        assert launcher._should_restart_console_probe_helper(
            launcher.MAX_CONSOLE_PROBE_RESTARTS
        ) is False
