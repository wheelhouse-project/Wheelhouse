"""Tests for ConsoleProbeClient -- the IPC client to the console-probe helper.

The client owns a persistent helper subprocess (a ``subprocess.Popen`` with
``stdin=PIPE, stdout=PIPE, stderr=DEVNULL``). It exposes
``is_at_prompt(process_name, pid) -> bool`` -- the SAME signature the
production ``FocusRedirectPolicy`` injects as ``prompt_detector_call`` -- and
serialises a request to the helper's stdin, reads the response from stdout,
and returns a bool.

The fix moves all AttachConsole/GetConsoleMode work out of the Logic process
into the helper subprocess, so the Logic process never binds to a foreign
terminal's console. These tests drive the client against a fake transport so
they run on any platform and assert the contract:

  * request is written to the helper's stdin carrying the pid
  * the True/False result from the helper response is returned
  * a read timeout raises ConsoleProbeError (transport failure, distinct from a
    real busy verdict so the policy can route it to prompt_detector_error --
    wh-jvrs.3.1)
  * a crash / EOF on the helper pipe raises ConsoleProbeError (graceful degrade,
    not a False)
  * the 0.2s per-pid cache prevents a second IPC round-trip within the TTL
"""

import io
import json
import logging
import subprocess

import pytest

from ui.console_probe_client import (
    ConsoleProbeClient,
    ConsoleProbeError,
    _CACHE_TTL,
)


class _FakeStdin:
    """Collects bytes written to the helper's stdin."""

    def __init__(self):
        self.buffer = io.BytesIO()
        self.flush_count = 0
        self.closed = False

    def write(self, data):
        return self.buffer.write(data)

    def flush(self):
        self.flush_count += 1

    def close(self):
        self.closed = True


class _FakeStdout:
    """Yields queued response lines; an empty queue simulates EOF."""

    def __init__(self, lines=None):
        self._lines = list(lines or [])
        self.closed = False

    def readline(self):
        if not self._lines:
            return b""  # EOF
        return self._lines.pop(0)

    def close(self):
        self.closed = True


class _FakeProc:
    """Stand-in for ``subprocess.Popen`` with controllable pipes."""

    def __init__(self, stdout_lines=None, alive=True):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(stdout_lines)
        self._alive = alive
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


def _response_line(pid, result):
    return (json.dumps({"pid": pid, "result": result}) + "\n").encode("utf-8")


def _client_with(proc, **kwargs):
    """Build a client whose factory returns ``proc`` (the fake helper)."""
    return ConsoleProbeClient(proc_factory=lambda: proc, **kwargs)


class TestConsoleProbeClientRequest:
    def test_sends_pid_and_returns_true(self):
        proc = _FakeProc(stdout_lines=[_response_line(1234, True)])
        client = _client_with(proc)

        result = client.is_at_prompt("pwsh.exe", 1234)

        assert result is True
        # The request written to stdin must carry the pid.
        written = proc.stdin.buffer.getvalue().decode("utf-8")
        payload = json.loads(written.strip())
        assert payload["pid"] == 1234

    def test_returns_false_result_from_helper(self):
        proc = _FakeProc(stdout_lines=[_response_line(1234, False)])
        client = _client_with(proc)

        assert client.is_at_prompt("pwsh.exe", 1234) is False


class TestConsoleProbeClientTimeout:
    def test_read_timeout_raises_console_probe_error(self):
        # A read timeout is a TRANSPORT failure, not a busy verdict: it must
        # raise ConsoleProbeError so FocusRedirectPolicy can route it to
        # prompt_detector_error instead of caching it as terminal_busy
        # (wh-jvrs.3.1).
        proc = _FakeProc(stdout_lines=[_response_line(1234, True)])

        def _timeout_reader(stdout, timeout_s):
            raise TimeoutError("helper response timed out")

        client = _client_with(proc, read_line=_timeout_reader)

        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 1234)


class TestConsoleProbeClientCrash:
    def test_eof_raises_console_probe_error(self):
        # Empty stdout -> readline returns b"" (EOF) -> transport failure.
        proc = _FakeProc(stdout_lines=[])
        client = _client_with(proc)

        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 1234)

    def test_ioerror_raises_console_probe_error(self):
        proc = _FakeProc(stdout_lines=[_response_line(1234, True)])

        def _raising_reader(stdout, timeout_s):
            raise IOError("broken pipe")

        client = _client_with(proc, read_line=_raising_reader)

        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 1234)

    def test_bad_json_raises_console_probe_error(self):
        proc = _FakeProc(stdout_lines=[b"not-json\n"])
        client = _client_with(proc)

        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 1234)

    def test_dead_helper_raises_console_probe_error_without_crashing(self):
        proc = _FakeProc(stdout_lines=[_response_line(1234, True)], alive=False)
        client = _client_with(proc)

        # A dead helper (poll() != None) cannot complete a round-trip: it is a
        # transport failure (ConsoleProbeError), not a False busy verdict, and
        # must not crash with anything other than ConsoleProbeError.
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 1234)


class TestConsoleProbeClientCache:
    def test_cache_prevents_second_ipc_within_ttl(self):
        # Only one response queued; a second call within the TTL must NOT
        # need a second response (it is served from cache).
        proc = _FakeProc(stdout_lines=[_response_line(1234, True)])

        call_count = {"n": 0}
        real_readline = proc.stdout.readline

        def _counting_reader(stdout, timeout_s):
            call_count["n"] += 1
            line = real_readline()
            if not line:
                raise EOFError
            return line

        client = _client_with(proc, read_line=_counting_reader)

        first = client.is_at_prompt("pwsh.exe", 1234)
        second = client.is_at_prompt("pwsh.exe", 1234)

        assert first is True
        assert second is True
        # The read transport ran exactly once; the second call hit the cache.
        assert call_count["n"] == 1

    def test_cache_expires_after_ttl(self, monkeypatch):
        proc = _FakeProc(
            stdout_lines=[
                _response_line(1234, True),
                _response_line(1234, False),
            ]
        )

        fake_now = {"t": 1000.0}
        monkeypatch.setattr(
            "ui.console_probe_client.time.monotonic",
            lambda: fake_now["t"],
        )

        client = _client_with(proc)

        first = client.is_at_prompt("pwsh.exe", 1234)
        # Advance past the cache TTL so the second call re-probes.
        fake_now["t"] += _CACHE_TTL + 0.01
        second = client.is_at_prompt("pwsh.exe", 1234)

        assert first is True
        assert second is False

    def test_different_pid_not_served_from_cache(self):
        proc = _FakeProc(
            stdout_lines=[
                _response_line(1234, True),
                _response_line(5678, False),
            ]
        )
        client = _client_with(proc)

        assert client.is_at_prompt("pwsh.exe", 1234) is True
        assert client.is_at_prompt("pwsh.exe", 5678) is False


class _MissingPipeProc:
    """Stand-in for an alive helper whose stdin or stdout pipe is None.

    Models a malformed or partially-created live process: ``poll()`` reports
    alive (None) so ``_ensure_proc`` stores it, but one of the pipes never
    came up. The missing-pipe branch must recycle this proc rather than leave
    it stored, or the next probe re-fetches the same bad helper and wedges the
    detector forever (wh-jvrs.3.3).
    """

    def __init__(self, *, missing="stdin"):
        self.stdin = None if missing == "stdin" else _FakeStdin()
        self.stdout = None if missing == "stdout" else _FakeStdout([])
        self._alive = True
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


class TestConsoleProbeClientMissingPipe:
    @pytest.mark.parametrize("missing", ["stdin", "stdout"])
    def test_missing_pipe_raises_console_probe_error(self, missing):
        # An alive helper with a None pipe cannot complete a round-trip: it is
        # a transport failure (ConsoleProbeError), not a False busy verdict.
        proc = _MissingPipeProc(missing=missing)
        client = _client_with(proc)

        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 1234)

    @pytest.mark.parametrize("missing", ["stdin", "stdout"])
    def test_missing_pipe_recycles_so_next_probe_respawns(self, missing):
        # The bad helper appears alive via poll(), so without recycling the
        # next probe would re-fetch the SAME stored proc and raise forever
        # (a permanently wedged detector). The fix recycles the helper on the
        # missing-pipe branch, so the next probe spawns a fresh one that can
        # answer (wh-jvrs.3.3).
        procs = [
            _MissingPipeProc(missing=missing),
            _FakeProc(stdout_lines=[_response_line(1234, True)]),
        ]
        spawn_count = {"n": 0}

        def _factory():
            proc = procs[spawn_count["n"]]
            spawn_count["n"] += 1
            return proc

        client = ConsoleProbeClient(proc_factory=_factory)

        # First probe hits the missing-pipe proc and raises a transport error.
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 1234)

        # The bad helper must have been recycled (torn down + cleared), so the
        # next probe spawns the healthy second helper and succeeds rather than
        # re-raising on the same wedged proc.
        assert procs[0].terminated is True
        assert client.is_at_prompt("pwsh.exe", 1234) is True
        assert spawn_count["n"] == 2


class TestConsoleProbeClientPidValidation:
    def test_mismatched_response_pid_raises_console_probe_error(self):
        # Helper echoes the WRONG pid -> treated as a transport fault, so it
        # raises ConsoleProbeError rather than returning a (mis-attributed)
        # busy/at-prompt bool (wh-jvrs.3.1).
        proc = _FakeProc(stdout_lines=[_response_line(9999, True)])
        client = _client_with(proc)

        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 1234)


class TestConsoleProbeClientResultValidation:
    """A returned bool must come from a genuine bool in the response.

    Regression guard for wh-jvrs.3.5: the client used ``bool(payload["result"])``
    after validating only the pid, so a malformed value such as the STRING
    ``"false"`` (truthy) or a numeric ``1`` would be returned as a real
    at-prompt=True answer and could open the terminal editor on a corrupted
    transport response. A non-bool result is a transport fault: recycle the
    helper and raise ConsoleProbeError so the policy routes it to
    prompt_detector_error.
    """

    @pytest.mark.parametrize(
        "bad_result",
        [
            "false",  # truthy string -> would have returned True
            "true",
            1,  # numeric truthy
            0,  # numeric falsy (still not a bool)
            None,
            [],
            {},
        ],
    )
    def test_non_bool_result_raises_console_probe_error(self, bad_result):
        proc = _FakeProc(stdout_lines=[_response_line(1234, bad_result)])
        client = _client_with(proc)

        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 1234)

    def test_missing_result_key_raises_console_probe_error(self):
        # A response with the pid but no ``result`` key is malformed.
        line = (json.dumps({"pid": 1234}) + "\n").encode("utf-8")
        proc = _FakeProc(stdout_lines=[line])
        client = _client_with(proc)

        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 1234)

    def test_non_bool_result_recycles_so_next_probe_respawns(self):
        # A malformed result desyncs the pipe just like other transport faults:
        # the bad helper must be recycled so the next probe spawns a fresh one
        # that can answer, not re-fetch the wedged proc (wh-jvrs.3.5).
        procs = [
            _FakeProc(stdout_lines=[_response_line(1234, "false")]),
            _FakeProc(stdout_lines=[_response_line(1234, True)]),
        ]
        spawn_count = {"n": 0}

        def _factory():
            proc = procs[spawn_count["n"]]
            spawn_count["n"] += 1
            return proc

        client = ConsoleProbeClient(proc_factory=_factory)

        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 1234)

        assert procs[0].terminated is True
        # A transport failure is never cached, so re-probing the SAME pid spawns
        # the healthy second helper (which echoes pid 1234) rather than serving
        # a stale cache entry.
        assert client.is_at_prompt("pwsh.exe", 1234) is True
        assert spawn_count["n"] == 2

    def test_genuine_false_bool_still_returned(self):
        # Guard against over-tightening: a real bool False must still pass.
        proc = _FakeProc(stdout_lines=[_response_line(1234, False)])
        client = _client_with(proc)

        assert client.is_at_prompt("pwsh.exe", 1234) is False


class TestConsoleProbeClientRestartBudget:
    def test_respawn_is_bounded(self):
        # Every spawned helper is dead-on-arrival, so each probe forces a
        # respawn. The respawn budget must stop the spin after max_restarts.
        spawn_count = {"n": 0}

        def _dead_factory():
            spawn_count["n"] += 1
            return _FakeProc(stdout_lines=[], alive=False)

        client = ConsoleProbeClient(proc_factory=_dead_factory, max_restarts=2)

        # Many probes; the first spawn is free, then respawns are capped at 2.
        # Each dead-helper probe is a transport failure (ConsoleProbeError); the
        # subject under test is the bounded spawn count, not the return value.
        for _ in range(10):
            # Bypass the per-pid cache by using distinct pids.
            with pytest.raises(ConsoleProbeError):
                client.is_at_prompt("pwsh.exe", spawn_count["n"] + 1)

        # 1 initial spawn attempt + at most 2 budgeted respawns = 3 spawns max.
        assert spawn_count["n"] <= 3


class TestConsoleProbeClientClose:
    def test_close_terminates_subprocess(self):
        # A helper that answers one probe cleanly stays alive and stored, so
        # close() is the call that terminates it (a probe that EOFed would have
        # already recycled the proc and cleared it, testing the wrong thing).
        proc = _FakeProc(stdout_lines=[_response_line(1234, True)])
        client = _client_with(proc)
        # Force the helper to be spawned via a successful round-trip.
        assert client.is_at_prompt("pwsh.exe", 1234) is True

        client.close()

        assert proc.terminated or proc.killed


class _OneShotEchoProc:
    """Helper that answers ONE probe correctly (echoing the request pid), then
    EOFs on every later read -- so the next probe sees EOF and recycles it.

    This models a helper that works once and then dies, the shape that exposes
    the lifetime-vs-consecutive budget bug: each good reply must reset the
    respawn budget so the client recovers indefinitely (wh-jvrs.2.1).
    """

    def __init__(self):
        self.stdin = _FakeStdin()
        self.stdout = self  # be our own stdout so readline can see the request
        self._alive = True
        self._answered = False
        self.terminated = False
        self.killed = False

    def readline(self):
        if self._answered:
            return b""  # EOF on the second read
        # Echo back the pid the client just wrote so the response validates.
        written = self.stdin.buffer.getvalue().decode("utf-8").strip()
        pid = json.loads(written.splitlines()[-1])["pid"]
        self._answered = True
        # Die after answering: the NEXT probe's _ensure_proc sees poll()!=None
        # and respawns a fresh one-shot helper, so every probe gets a clean
        # helper that answers and resets the budget.
        self._alive = False
        return _response_line(pid, True)

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def close(self):
        pass

    def wait(self, timeout=None):
        self._alive = False
        return 0


class TestConsoleProbeClientBudgetReset:
    def test_successful_probe_resets_respawn_budget(self):
        # Each spawned helper answers exactly one probe (correct pid), then
        # EOFs -> the next probe recycles it. Because every good reply resets
        # the budget, the client must keep recovering across far more than
        # max_restarts LIFETIME deaths instead of going permanently dark
        # (wh-jvrs.2.1).
        spawn_count = {"n": 0}

        def _oneshot_factory():
            spawn_count["n"] += 1
            return _OneShotEchoProc()

        client = ConsoleProbeClient(proc_factory=_oneshot_factory, max_restarts=2)

        good = 0
        for i in range(1, 21):  # distinct pids bypass the per-pid cache
            if client.is_at_prompt("pwsh.exe", i):
                good += 1

        # Every probe succeeds (the budget never exhausts), and many respawns
        # happened -- far more than max_restarts (2) + the free first spawn.
        assert good == 20
        assert spawn_count["n"] > 3

    def test_budget_still_caps_consecutive_failures(self):
        # No successful probe ever happens (every helper is dead-on-arrival),
        # so the budget is never reset and the consecutive-failure cap holds
        # exactly as before (regression guard for wh-jvrs.2.1's reset).
        spawn_count = {"n": 0}

        def _dead_factory():
            spawn_count["n"] += 1
            return _FakeProc(stdout_lines=[], alive=False)

        client = ConsoleProbeClient(proc_factory=_dead_factory, max_restarts=2)

        for i in range(10):
            # Dead-on-arrival helper -> transport failure -> ConsoleProbeError.
            with pytest.raises(ConsoleProbeError):
                client.is_at_prompt("pwsh.exe", i + 1)

        # 1 initial spawn + at most 2 budgeted respawns = 3 spawns max.
        # (These 10 probes run well within the default degrade cooldown, so no
        # recovery spawn fires -- the cap holds exactly as before.)
        assert spawn_count["n"] <= 3


class TestConsoleProbeClientDegradeRecovery:
    """Once the respawn budget exhausts, the probe must not go permanently
    dark for the whole process run (wh-console-probe-degrade).

    The old behaviour: ``_ensure_proc`` returned None forever and logged the
    exhaustion ERROR on EVERY refused call (~14 times in the field run, each
    firing a Windows notification). The fix: enter a DEGRADED episode that
    logs the ERROR exactly once, refuses to spawn during a cooldown, then
    allows one recovery spawn per cooldown so a busy spell self-heals.
    """

    def test_degraded_probe_refuses_within_cooldown_then_retries_after(
        self, monkeypatch
    ):
        fake_now = {"t": 1000.0}
        monkeypatch.setattr(
            "ui.console_probe_client.time.monotonic", lambda: fake_now["t"]
        )

        spawn_count = {"n": 0}

        def _dead_factory():
            spawn_count["n"] += 1
            return _FakeProc(stdout_lines=[], alive=False)

        client = ConsoleProbeClient(
            proc_factory=_dead_factory, max_restarts=2, degraded_cooldown_s=5.0
        )

        # Burn the budget: 1 free spawn + 2 respawns = 3 spawns.
        for i in range(3):
            with pytest.raises(ConsoleProbeError):
                client.is_at_prompt("pwsh.exe", i + 1)
        assert spawn_count["n"] == 3

        # Next probe starts the degraded episode: refused, NO new spawn.
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 100)
        assert spawn_count["n"] == 3

        # Still within the cooldown: still refused, still no spawn.
        fake_now["t"] += 1.0
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 101)
        assert spawn_count["n"] == 3

        # Past the cooldown: exactly ONE recovery spawn is attempted.
        fake_now["t"] += 5.0
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 102)
        assert spawn_count["n"] == 4

    def test_degraded_error_logged_once_per_episode(self, monkeypatch, caplog):
        fake_now = {"t": 500.0}
        monkeypatch.setattr(
            "ui.console_probe_client.time.monotonic", lambda: fake_now["t"]
        )

        def _dead_factory():
            return _FakeProc(stdout_lines=[], alive=False)

        client = ConsoleProbeClient(
            proc_factory=_dead_factory, max_restarts=1, degraded_cooldown_s=10.0
        )

        caplog.set_level(logging.ERROR, logger="ui.console_probe_client")

        # 1 free spawn + 1 respawn exhausts the budget; then many refused
        # probes, all inside the 10s cooldown (0.5s apart * 8 = 4s < 10s).
        for i in range(8):
            with pytest.raises(ConsoleProbeError):
                client.is_at_prompt("pwsh.exe", i + 1)
            fake_now["t"] += 0.5

        degraded_errors = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and "degraded" in r.getMessage()
        ]
        assert len(degraded_errors) == 1

    def test_healthy_helper_after_cooldown_fully_recovers(self, monkeypatch):
        fake_now = {"t": 0.0}
        monkeypatch.setattr(
            "ui.console_probe_client.time.monotonic", lambda: fake_now["t"]
        )

        procs = []

        def _factory():
            # First 3 helpers are dead-on-arrival; the recovery spawn is healthy.
            if len(procs) < 3:
                p = _FakeProc(stdout_lines=[], alive=False)
            else:
                p = _FakeProc(stdout_lines=[_response_line(777, True)])
            procs.append(p)
            return p

        client = ConsoleProbeClient(
            proc_factory=_factory, max_restarts=2, degraded_cooldown_s=5.0
        )

        # 3 dead spawns exhaust the budget.
        for i in range(3):
            with pytest.raises(ConsoleProbeError):
                client.is_at_prompt("pwsh.exe", i + 1)
        # Next probe starts the degraded episode (no spawn).
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 50)
        assert len(procs) == 3

        # Past the cooldown, the one recovery spawn is the healthy helper and
        # answers, so the probe recovers end-to-end.
        fake_now["t"] += 6.0
        assert client.is_at_prompt("pwsh.exe", 777) is True
        assert len(procs) == 4

    def test_recovery_success_resets_budget_for_a_fresh_degrade(
        self, monkeypatch
    ):
        # After a successful recovery the budget must be fully cleared: a later
        # spell of failures has to burn the WHOLE budget again before it can
        # re-degrade (not degrade on the very first new failure).
        fake_now = {"t": 0.0}
        monkeypatch.setattr(
            "ui.console_probe_client.time.monotonic", lambda: fake_now["t"]
        )

        script = {"healthy": False}
        spawn_count = {"n": 0}

        def _factory():
            spawn_count["n"] += 1
            if script["healthy"]:
                return _FakeProc(stdout_lines=[_response_line(999, True)])
            return _FakeProc(stdout_lines=[], alive=False)

        client = ConsoleProbeClient(
            proc_factory=_factory, max_restarts=2, degraded_cooldown_s=5.0
        )

        # Exhaust the budget, enter the degraded episode.
        for i in range(3):
            with pytest.raises(ConsoleProbeError):
                client.is_at_prompt("pwsh.exe", i + 1)
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 50)

        # Recover with a healthy helper.
        script["healthy"] = True
        fake_now["t"] += 6.0
        assert client.is_at_prompt("pwsh.exe", 999) is True
        spawns_after_recovery = spawn_count["n"]

        # Now go back to dead helpers. Because the budget was reset, it takes a
        # fresh free spawn + max_restarts respawns before the next degrade --
        # i.e. more than one failing probe before it stops spawning.
        script["healthy"] = False
        fake_now["t"] += 6.0  # well past the cooldown so no stale gating
        for i in range(3):
            with pytest.raises(ConsoleProbeError):
                client.is_at_prompt("pwsh.exe", 1000 + i)

        # The healthy helper died on its next probe (EOF) and every helper
        # since was dead-on-arrival, so the budget was consumed again from
        # zero: strictly more than one spawn happened after recovery.
        assert spawn_count["n"] - spawns_after_recovery >= 2

    def test_failed_recovery_stays_degraded_with_backoff(self, monkeypatch):
        # A FAILED recovery must not re-open the budget: the probe stays
        # degraded and allows at most one spawn per cooldown, and the cooldown
        # DOUBLES after a failed recovery. Without this guard the original spin
        # bug (a failed recovery re-opening the full budget) would pass unnoticed
        # (wh-console-probe-degrade.1.3 backoff, .1.4 test gap).
        fake_now = {"t": 1000.0}
        monkeypatch.setattr(
            "ui.console_probe_client.time.monotonic", lambda: fake_now["t"]
        )

        spawn_count = {"n": 0}

        def _dead_factory():
            spawn_count["n"] += 1
            return _FakeProc(stdout_lines=[], alive=False)

        client = ConsoleProbeClient(
            proc_factory=_dead_factory, max_restarts=2, degraded_cooldown_s=5.0
        )

        # Burn the budget (3 spawns), then start the degraded episode.
        for i in range(3):
            with pytest.raises(ConsoleProbeError):
                client.is_at_prompt("pwsh.exe", i + 1)
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 100)
        assert spawn_count["n"] == 3

        # First recovery at the base 5s cooldown: exactly one spawn, which fails.
        fake_now["t"] += 5.0
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 101)
        assert spawn_count["n"] == 4

        # 5s after the failed recovery: the OLD 5s cooldown would allow another
        # spawn, but backoff doubled it to 10s, so this is still refused with NO
        # new spawn. This is the invariant .1.4 flagged: a failed recovery stays
        # degraded and does not re-open the budget.
        fake_now["t"] += 5.0
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 102)
        assert spawn_count["n"] == 4

        # 10s after the failed recovery the doubled cooldown elapses: exactly
        # ONE more recovery spawn is allowed.
        fake_now["t"] += 5.0
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 103)
        assert spawn_count["n"] == 5

    def test_failed_live_recovery_via_probe_teardown_stays_degraded(
        self, monkeypatch
    ):
        # Same invariant as test_failed_recovery_stays_degraded_with_backoff,
        # but the recovery spawn produces a LIVE helper that then FAILS its
        # round-trip (empty stdout -> EOF) inside _probe. That helper is torn
        # down through _probe -> _kill_proc_locked, a DIFFERENT path than the
        # dead-on-arrival recovery _ensure_proc discards directly. Neither path
        # calls _reset_respawn_budget_locked (only a fully validated round-trip
        # does), so the degrade state must be preserved on this path too: the
        # probe stays degraded and the cooldown keeps doubling. A regression
        # that reset the budget inside _kill_proc_locked (or any _probe failure
        # handler) would re-open the full budget on every failed live recovery
        # -- the exact spin the backoff prevents -- and would break the
        # spawn_count == 4 assertion below (wh-console-probe-degrade.2.1).
        fake_now = {"t": 1000.0}
        monkeypatch.setattr(
            "ui.console_probe_client.time.monotonic", lambda: fake_now["t"]
        )

        spawn_count = {"n": 0}
        procs = []

        def _factory():
            spawn_count["n"] += 1
            # First 3 spawns burn the budget (dead-on-arrival, discarded by
            # _ensure_proc); every recovery spawn after that is a LIVE helper
            # whose empty stdout fails the round-trip (EOF) inside _probe.
            alive = spawn_count["n"] > 3
            p = _FakeProc(stdout_lines=[], alive=alive)
            procs.append(p)
            return p

        client = ConsoleProbeClient(
            proc_factory=_factory, max_restarts=2, degraded_cooldown_s=5.0
        )

        # Burn the budget (3 spawns), then start the degraded episode.
        for i in range(3):
            with pytest.raises(ConsoleProbeError):
                client.is_at_prompt("pwsh.exe", i + 1)
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 100)
        assert spawn_count["n"] == 3

        # First recovery at the base 5s cooldown: exactly one LIVE spawn, whose
        # round-trip fails via EOF and is torn down through _kill_proc_locked.
        fake_now["t"] += 5.0
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 101)
        assert spawn_count["n"] == 4
        # The recovery helper really was live and went through the _probe
        # teardown (terminate), not the dead-on-arrival discard: poll() was
        # None when it was stored and probed.
        assert procs[-1].terminated is True

        # 5s after the failed live recovery: the OLD 5s cooldown would allow
        # another spawn, but backoff doubled it to 10s, so this is still refused
        # with NO new spawn. This is the invariant .2.1 flagged for the
        # live-teardown path: a failed live recovery does not re-open the budget.
        fake_now["t"] += 5.0
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 102)
        assert spawn_count["n"] == 4

        # 10s after the failed recovery the doubled cooldown elapses: exactly
        # ONE more recovery spawn is allowed.
        fake_now["t"] += 5.0
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 103)
        assert spawn_count["n"] == 5

    def test_degraded_recovery_spawn_exception_stays_quiet(
        self, monkeypatch, caplog
    ):
        # A recovery spawn whose FACTORY RAISES (e.g. OSError creating the
        # subprocess on a loaded machine -- the busy-host case this whole change
        # targets) must not re-log an ERROR at each backoff step. The committed
        # design logs the exhaustion ERROR once per episode and keeps per-attempt
        # recovery logs at DEBUG, so a persistent raising factory does not fire a
        # Windows notification at the 5s/10s/20s/60s cadence. Without the guard
        # the spawn-failure logger.exception in _ensure_proc fires on every
        # failed recovery attempt in the episode (wh-console-probe-degrade.3.1).
        fake_now = {"t": 1000.0}
        monkeypatch.setattr(
            "ui.console_probe_client.time.monotonic", lambda: fake_now["t"]
        )

        def _raising_factory():
            raise OSError("cannot spawn helper on a loaded machine")

        client = ConsoleProbeClient(
            proc_factory=_raising_factory, max_restarts=2, degraded_cooldown_s=5.0
        )
        caplog.set_level(logging.DEBUG, logger="ui.console_probe_client")

        # Burn the budget (free spawn + 2 respawns, all raising), then the next
        # probe starts the degraded episode (the single exhaustion ERROR).
        for i in range(4):
            with pytest.raises(ConsoleProbeError):
                client.is_at_prompt("pwsh.exe", i + 1)

        # Exactly one exhaustion ERROR marks the episode start.
        exhaustion_errors = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and "degraded" in r.getMessage()
        ]
        assert len(exhaustion_errors) == 1
        errors_before = len(
            [r for r in caplog.records if r.levelno >= logging.ERROR]
        )

        # Two recovery attempts across two cooldown steps, both raising in the
        # factory. Neither may add an ERROR record: the recovery spawn failure
        # is expected during a degraded episode and must log at DEBUG.
        fake_now["t"] += 5.0
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 100)
        fake_now["t"] += 10.0
        with pytest.raises(ConsoleProbeError):
            client.is_at_prompt("pwsh.exe", 101)

        errors_after = len(
            [r for r in caplog.records if r.levelno >= logging.ERROR]
        )
        assert errors_after == errors_before
        # The exhaustion ERROR is still logged only once across the episode.
        exhaustion_errors = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and "degraded" in r.getMessage()
        ]
        assert len(exhaustion_errors) == 1


class _RecStream:
    """A pipe stream whose close() records into a shared ordered event list."""

    def __init__(self, name, events):
        self.name = name
        self.events = events
        self.closed = False

    def close(self):
        self.events.append(f"close:{self.name}")
        self.closed = True


class _RecProc:
    """Records the ORDER of terminate/kill/wait/close so the teardown contract
    can be asserted: the process must be terminated and reaped BEFORE its pipe
    ends are closed, so a reader thread blocked in readline gets EOF from the
    helper's death rather than being abandoned (wh-console-probe-degrade)."""

    def __init__(self, *, stubborn=False):
        self.events = []
        self.stdin = _RecStream("stdin", self.events)
        self.stdout = _RecStream("stdout", self.events)
        self._alive = True
        self._stubborn = stubborn
        self._waited = 0

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.events.append("terminate")
        if not self._stubborn:
            self._alive = False

    def kill(self):
        self.events.append("kill")
        self._alive = False

    def wait(self, timeout=None):
        self._waited += 1
        self.events.append("wait")
        # A stubborn helper ignores terminate: the first reap times out, which
        # must escalate to kill(); the post-kill reap then succeeds.
        if self._stubborn and self._waited == 1:
            raise subprocess.TimeoutExpired(cmd="helper", timeout=timeout)
        self._alive = False
        return 0


class TestConsoleProbeClientDiscardOrdering:
    def test_discard_terminates_and_reaps_before_closing_pipes(self):
        proc = _RecProc()

        # The reap + close runs in a detached daemon thread (kept off the
        # caller's lock, wh-console-probe-degrade.1.2); join it before asserting.
        reaper = ConsoleProbeClient._discard_proc(proc)
        reaper.join(timeout=2.0)
        assert not reaper.is_alive()

        # Terminate first so the helper's death closes its write end (EOF to a
        # blocked reader); reap it; only THEN close our pipe ends.
        assert proc.events[0] == "terminate"
        assert "wait" in proc.events
        assert proc.events.index("terminate") < proc.events.index("wait")
        assert proc.events.index("wait") < proc.events.index("close:stdin")
        assert proc.events.index("wait") < proc.events.index("close:stdout")
        assert proc.stdin.closed and proc.stdout.closed

    def test_discard_escalates_to_kill_when_terminate_ignored(self):
        proc = _RecProc(stubborn=True)

        reaper = ConsoleProbeClient._discard_proc(proc)
        reaper.join(timeout=2.0)
        assert not reaper.is_alive()

        assert "terminate" in proc.events
        assert "kill" in proc.events
        assert proc.events.index("terminate") < proc.events.index("kill")
        # terminate -> wait (times out) -> kill -> wait (reaps).
        assert proc.events.count("wait") >= 2
        assert proc.stdin.closed and proc.stdout.closed

    def test_discard_returns_promptly_without_blocking_on_reap(self):
        # The point of wh-console-probe-degrade.1.2: _discard_proc must NOT block
        # the caller on the reap. A proc whose wait() always times out is handed
        # to the daemon thread; _discard_proc returns a live thread and the
        # blocking (and kill-escalation) happens off the caller's lock.
        class _SlowWaitProc(_RecProc):
            def wait(self, timeout=None):
                self.events.append("wait")
                raise subprocess.TimeoutExpired(cmd="helper", timeout=timeout)

        proc = _SlowWaitProc()
        reaper = ConsoleProbeClient._discard_proc(proc)

        # terminate ran synchronously in the caller; the reap is a joinable thread.
        assert "terminate" in proc.events
        assert reaper is not None
        reaper.join(timeout=2.0)
        assert not reaper.is_alive()
        # Every wait() raised, so the reaper escalated to kill and still closed.
        assert "kill" in proc.events
        assert proc.stdin.closed and proc.stdout.closed
