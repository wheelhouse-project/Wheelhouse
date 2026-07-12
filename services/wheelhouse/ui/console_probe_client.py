"""IPC client to the console-probe helper subprocess (wh-jvrs.1).

``ConsoleProbeClient`` is the Logic-process side of the console-probe split.
It owns a persistent helper subprocess (``ui/console_probe_helper.py``) and
exposes ``is_at_prompt(process_name, pid) -> bool`` -- the exact signature the
production ``FocusRedirectPolicy`` injects as ``prompt_detector_call``. The
client serialises a JSON request to the helper's stdin and reads a JSON
response from its stdout; the helper, in its own isolated process, owns every
``AttachConsole`` call, so the Logic process never binds to a foreign console.

Why this is shaped the way it is:

  * **0.2s per-pid cache.** Preserves the original ``PromptDetector`` cache TTL
    so a burst of dictated words for the same shell does not fan out into one
    IPC round-trip per word. Keyed on pid (the process_name is advisory only).
  * **Bounded read timeout (default 0.4s).** Leaves margin inside the policy's
    0.5s detector budget. The policy already runs this callable off-loop on a
    single-worker thread pool and bounds it with ``asyncio.wait_for``; the
    client's own timeout is a second fence so a wedged helper cannot block the
    worker thread indefinitely.
  * **Transport failures are out-of-band, never a False.** Timeout, broken
    pipe, EOF, malformed JSON, a pid mismatch, a missing or non-bool
    ``result`` value, or a dead helper all raise ``ConsoleProbeError`` -- they
    do NOT return False. A malformed ``result`` (the string ``"false"``, a
    numeric ``1``) is truthy under ``bool()`` and would otherwise fabricate an
    at-prompt=True answer from a corrupted response (wh-jvrs.3.5). This is the wh-jvrs.3.1
    fix: ``FocusRedirectPolicy`` must be able to tell a transport failure apart
    from a real "shell is busy" answer. A bare False from a transient helper
    stall was being cached by the policy as ``terminal_busy``, suppressing the
    terminal-editor redirect for the whole utterance even when the terminal was
    actually at a prompt. By raising instead, the policy routes the failure to
    its ``prompt_detector_error`` path (one of the spec's named failure paths,
    wh-jvrs.1) and does not poison the utterance cache with a fake busy verdict.
    A returned bool therefore means exactly one thing: ``True`` = at a prompt,
    ``False`` = shell is busy. Both come only from a complete, validated
    request/response round-trip.
  * **Crash recovery.** If the helper dies mid-session, the next call detects
    the dead process (``poll() is not None``) or hits EOF/IOError. That call
    recycles the dead helper and raises ``ConsoleProbeError`` (the same
    transport-failure contract as every other failure path above -- it does
    NOT return False), and the helper is lazily respawned on the FOLLOWING
    call so a later utterance can succeed. The recycle terminates and REAPS the
    helper before closing our pipe ends, so its death delivers EOF to any
    reader thread still blocked in ``readline`` instead of abandoning it
    (Windows does not reliably wake a blocked read by closing our end).
  * **Degrade recovery.** A run of consecutive respawns that exhausts the
    budget no longer disables the probe for the whole process run. The probe
    enters a DEGRADED episode: it fails closed during a cooldown, logs the
    exhaustion ERROR exactly once (not once per refused call), then allows one
    recovery spawn per cooldown so a busy-terminal spell self-heals without an
    app restart (wh-console-probe-degrade).

Threading: the policy serialises detector calls on its single-worker executor,
so ``is_at_prompt`` is effectively single-threaded per client. A lock still
guards the spawn/respawn and the request/response exchange so a stray
concurrent caller cannot interleave two requests on the one pipe.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class ConsoleProbeError(Exception):
    """Raised when the at-prompt probe could not complete a round-trip.

    Signals a TRANSPORT failure -- a read timeout, broken pipe, EOF, malformed
    response, response-pid mismatch, or a helper that could not be (re)spawned.
    It is deliberately distinct from a successful probe that simply answers
    "shell is busy" (a returned ``False``): ``FocusRedirectPolicy`` routes this
    exception to its ``prompt_detector_error`` failure path instead of the
    ``terminal_busy`` verdict, so a transient helper stall cannot masquerade as
    a real busy shell and suppress the terminal-editor redirect for the rest of
    the utterance (wh-jvrs.3.1).
    """


# Per-pid cache TTL, matching the original PromptDetector cache (0.2s).
_CACHE_TTL = 0.2

# Default bound on the helper response read, in seconds. Sits comfortably
# inside the policy's 0.5s detector budget so a late helper still leaves the
# policy time to fail closed on its own deadline.
_DEFAULT_READ_TIMEOUT_S = 0.4

# Bound on each reap wait while discarding a helper. Small, because a
# terminated helper normally exits in milliseconds; the timeout only bites a
# stubborn helper, which we then escalate to kill(). Two of these (reap after
# terminate, reap after kill) is the worst-case teardown cost, paid only on the
# failure/recycle path.
_DISCARD_WAIT_S = 0.2

# How long the probe stays degraded after the respawn budget exhausts before it
# is allowed one recovery spawn. Gives a busy-terminal spell a way back without
# an app restart, while keeping the retry rate to at most one helper spawn per
# cooldown so a persistently wedged terminal cannot spin (wh-console-probe-degrade).
_DEGRADED_COOLDOWN_S = 5.0

# Ceiling for the exponential backoff of the degrade cooldown. Each failed
# recovery doubles the cooldown up to this cap, so a busy spell still recovers
# on the first 5s retry, but a terminal that is broken for the whole process run
# settles to at most one doomed spawn per minute instead of one every 5s -- a
# real reduction in churn versus an unbounded fixed-rate retry (wh-console-probe-degrade.1.3).
_DEGRADED_COOLDOWN_MAX_S = 60.0


def _default_helper_command() -> list[str]:
    """Return the argv that launches the helper as a module-less script.

    Single source of truth is ``launcher._console_probe_helper_command`` so the
    spawned helper path matches the supervision seam the launcher exposes
    (covered by ``tests/test_launcher_console_probe.py``). Falls back to a
    local build if the launcher module cannot be imported in a stripped-down
    test or headless context. Running the file directly (rather than ``-m``)
    keeps the helper independent of which package-root happens to be importable
    in the spawning environment.
    """
    try:
        from launcher import _console_probe_helper_command
        return _console_probe_helper_command()
    except Exception:
        helper_path = os.path.join(
            os.path.dirname(__file__), "console_probe_helper.py"
        )
        return [sys.executable, helper_path]


def _blocking_readline_with_timeout(stdout, timeout_s: float):
    """Read one line from ``stdout``, raising ``TimeoutError`` past the deadline.

    The default transport reads on a short-lived daemon thread so a wedged
    helper cannot block the caller forever. This is replaced wholesale by tests
    via the ``read_line`` injection point, so it carries no test-only branches.
    """
    result: dict = {}

    def _reader():
        try:
            result["line"] = stdout.readline()
        except Exception as exc:  # pragma: no cover - exercised via injection
            result["exc"] = exc

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError("helper response read timed out")
    if "exc" in result:
        raise result["exc"]
    line = result.get("line", b"")
    if not line:
        raise EOFError("helper closed its stdout (EOF)")
    return line


class ConsoleProbeClient:
    """Request/reply client for the console-probe helper subprocess."""

    def __init__(
        self,
        *,
        proc_factory: Optional[Callable[[], object]] = None,
        read_line: Optional[Callable[[object, float], object]] = None,
        read_timeout_s: float = _DEFAULT_READ_TIMEOUT_S,
        max_restarts: Optional[int] = None,
        degraded_cooldown_s: float = _DEGRADED_COOLDOWN_S,
    ) -> None:
        """Construct the client.

        Args:
          proc_factory: zero-arg callable returning a ``Popen``-like object with
            ``stdin``/``stdout`` pipes and ``poll()``/``terminate()``. Injected
            by tests; production passes None and a real helper is spawned with
            ``stderr`` discarded at the OS level.
          read_line: callable ``(stdout, timeout_s) -> bytes`` that reads one
            response line or raises (``TimeoutError``/``EOFError``/``IOError``).
            Injected by tests; production uses the threaded reader.
          read_timeout_s: bound on each response read.
          max_restarts: cap on CONSECUTIVE helper RESPAWNS (not the initial
            spawn) before the probe stays degraded (each call raises
            ``ConsoleProbeError`` rather than spawning) to stop a
            helper that crashes on every spawn from spinning forever
            (wh-jvrs.1.1.2). The count resets to 0 after any probe that
            completes a full request/response round-trip, so the budget bounds a
            tight respawn loop rather than lifetime failures across a long
            session (wh-jvrs.2.1). Defaults to
            ``launcher.MAX_CONSOLE_PROBE_RESTARTS`` so the launcher's budget is
            the single source of truth; falls back to 5 if that import fails.
          degraded_cooldown_s: after the respawn budget exhausts, how long the
            probe stays degraded (each call raises ``ConsoleProbeError`` without
            spawning) before it is allowed ONE recovery spawn. A recovery that
            fails re-arms the cooldown; a recovery that completes a round-trip
            clears the degraded state and the budget. This is what gives a busy
            spell a way back without an app restart, at a bounded retry rate of
            one spawn per cooldown (wh-console-probe-degrade). Injected small in
            tests.
        """
        self._proc_factory = proc_factory or self._spawn_real_helper
        self._read_line = read_line or _blocking_readline_with_timeout
        self._read_timeout_s = read_timeout_s
        self._proc = None
        # Per-pid cache: pid -> (result, monotonic_set_time).
        self._cache: dict[int, tuple[bool, float]] = {}
        self._lock = threading.Lock()
        # Respawn budget. Built once at construction (no spawn happens here).
        # The respawn-allowed predicate is the launcher seam so the budget rule
        # lives in one place; both default the cap from the launcher constant.
        if max_restarts is None:
            try:
                from launcher import MAX_CONSOLE_PROBE_RESTARTS
                max_restarts = MAX_CONSOLE_PROBE_RESTARTS
            except Exception:
                max_restarts = 5
        self._max_restarts = max_restarts
        # Counts CONSECUTIVE respawns consumed against the budget. The first
        # spawn attempt is free (``_spawned_once`` is False until then); every
        # later attempt consumes the budget. ``_probe`` resets this to 0 after
        # any successful round-trip so the cap bounds a tight respawn loop, not
        # lifetime failures across a long session (wh-jvrs.2.1).
        self._restart_count = 0
        self._spawned_once = False
        # Degraded-episode state. ``_degraded_since`` is the monotonic time the
        # current degraded episode began, or None while the probe is healthy.
        # It is set when the respawn budget first exhausts and cleared on any
        # successful round-trip; it gates the recovery cooldown AND the
        # once-per-episode ERROR log so a persistently busy terminal does not
        # fire a Windows-notification storm (wh-console-probe-degrade).
        self._degraded_cooldown_s = degraded_cooldown_s
        self._degraded_since: Optional[float] = None
        # Current cooldown for THIS episode. Starts at the base cooldown and
        # doubles on each failed recovery up to _DEGRADED_COOLDOWN_MAX_S, so a
        # permanently-broken terminal backs off instead of spawning a doomed
        # helper at a fixed fast rate forever (wh-console-probe-degrade.1.3).
        # Reset to the base on any successful round-trip.
        self._current_cooldown_s = degraded_cooldown_s

    def _restart_allowed(self) -> bool:
        """Return True iff a respawn is within budget.

        Reuses the launcher's predicate when this client carries the launcher's
        default cap (the production case) so the budget rule has one definition;
        a test-injected custom cap is checked locally.
        """
        try:
            from launcher import (
                MAX_CONSOLE_PROBE_RESTARTS,
                _should_restart_console_probe_helper,
            )
            if self._max_restarts == MAX_CONSOLE_PROBE_RESTARTS:
                return _should_restart_console_probe_helper(self._restart_count)
        except Exception:
            pass
        return self._restart_count < self._max_restarts

    def _enter_or_recover_degraded_locked(self) -> bool:
        """Gate a spawn request that has exhausted the respawn budget.

        Returns True iff a single recovery spawn is permitted on THIS call (the
        degrade cooldown has elapsed). The caller then spawns one fresh helper
        WITHOUT touching the budget; only a successful round-trip in ``_probe``
        clears the degraded state (via ``_reset_respawn_budget_locked``).
        Returns False while the probe should stay degraded -- the caller returns
        None so the probe raises ``ConsoleProbeError`` and the policy fails
        closed.

        The cooldown is what gives the degraded state a way back. Without it the
        budget could be cleared only by a successful probe, which could never
        happen because the exhausted budget refused to spawn -- a permanent
        dead-end for the whole process run until an app restart, which is exactly
        the field failure this fixes (wh-console-probe-degrade). The ERROR is
        logged exactly once per episode (on the None->set transition) so a
        persistently busy terminal does not fire a Windows-notification storm
        (the old code logged it on every refused call, ~14 times in the field
        run). A recovery attempt logs at INFO -- no notification. Caller must
        hold ``self._lock``.
        """
        now = time.monotonic()
        if self._degraded_since is None:
            # First refusal of this episode: start the cooldown clock at the
            # base cooldown and log the ERROR exactly once.
            self._degraded_since = now
            self._current_cooldown_s = self._degraded_cooldown_s
            logger.error(
                "console_probe_client: helper respawn budget exhausted "
                "(%d restarts); the at-prompt probe is degraded and will "
                "retry after a %.1fs cooldown.",
                self._restart_count, self._current_cooldown_s,
            )
            return False
        if (now - self._degraded_since) < self._current_cooldown_s:
            return False
        # Cooldown elapsed: permit ONE recovery spawn. Re-arm the clock so a
        # recovery that fails waits the next cooldown before retrying instead of
        # spinning, and DOUBLE the cooldown (capped) so a permanently-broken
        # terminal backs off to at most one doomed spawn per minute rather than
        # one every base-cooldown forever (wh-console-probe-degrade.1.3). The
        # budget is left at its exhausted value so only a successful round-trip
        # clears it. This is a per-attempt detail, so it logs at DEBUG -- the
        # once-per-episode ERROR above is the only user-visible signal.
        self._degraded_since = now
        self._current_cooldown_s = min(
            _DEGRADED_COOLDOWN_MAX_S, self._current_cooldown_s * 2
        )
        logger.debug(
            "console_probe_client: degrade cooldown elapsed; attempting one "
            "recovery spawn (next cooldown %.1fs).",
            self._current_cooldown_s,
        )
        return True

    def _reset_respawn_budget_locked(self) -> None:
        """Clear the respawn budget and any degraded episode after a healthy
        round-trip, so the budget bounds CONSECUTIVE failures rather than
        lifetime failures across a long session (wh-jvrs.2.1) and a recovered
        probe can degrade freshly later (wh-console-probe-degrade). Caller must
        hold ``self._lock``.
        """
        # Log a single INFO only on the degraded->healthy transition so a
        # recovery is visible without spamming a line on every healthy probe.
        if self._degraded_since is not None:
            logger.info(
                "console_probe_client: at-prompt probe recovered from the "
                "degraded state."
            )
        self._restart_count = 0
        self._degraded_since = None
        self._current_cooldown_s = self._degraded_cooldown_s

    # --- helper lifecycle ---------------------------------------------------

    @staticmethod
    def _spawn_real_helper():
        """Spawn the helper with stdin/stdout pipes and stderr discarded.

        Delegates to ``launcher._spawn_console_probe_helper`` so production has a
        single spawn path (covered by ``tests/test_launcher_console_probe.py``)
        that guarantees ``stderr=DEVNULL`` -- the OS-level fence that stops the
        helper leaking text to a foreign terminal even if a library inside it
        writes to stderr. Falls back to a local ``Popen`` if the launcher module
        cannot be imported in a stripped-down context.
        """
        try:
            from launcher import _spawn_console_probe_helper
            return _spawn_console_probe_helper()
        except Exception:
            return subprocess.Popen(
                _default_helper_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )

    @staticmethod
    def _proc_alive(proc) -> bool:
        """Return True iff ``proc`` is a live helper.

        Reuses the launcher's liveness predicate so the "is the helper alive?"
        rule has one definition shared by the launcher and the client; falls
        back to a direct ``poll()`` check if the launcher import fails.
        """
        try:
            from launcher import _console_probe_helper_alive
            return _console_probe_helper_alive(proc)
        except Exception:
            if proc is None:
                return False
            try:
                return proc.poll() is None
            except Exception:
                return False

    def _ensure_proc(self):
        """Return a live helper process, spawning/respawning as needed.

        Returns None when the helper cannot be (re)spawned; the caller
        (``_probe``) turns that None into a raised ``ConsoleProbeError``
        rather than a False busy verdict (wh-jvrs.3.1). Every spawn ATTEMPT
        after the first is bounded by the restart budget so a helper that dies
        on every spawn (or is dead-on-arrival) cannot spin forever. Past the
        budget the probe enters a DEGRADED episode (``_enter_or_recover_degraded_locked``):
        it returns None during a cooldown, then allows one recovery spawn per
        cooldown, so a busy spell self-heals instead of disabling the probe for
        the whole process run (wh-console-probe-degrade). Counting attempts --
        not just respawns of a stored process -- is deliberate: a
        dead-on-arrival helper never gets stored, so a respawn-of-stored-only
        counter would never fire and the spin would be unbounded.
        """
        proc = self._proc
        if proc is not None and self._proc_alive(proc):
            return proc
        # Dead or never-spawned: drop the corpse before spawning a fresh one.
        if proc is not None:
            self._discard_proc(proc)
            self._proc = None
        # The first spawn attempt of this client's life is free; every later
        # attempt consumes the respawn budget. Past the budget the probe is
        # DEGRADED: it refuses to spawn until a cooldown elapses, then allows
        # exactly one recovery spawn so a busy spell self-heals rather than
        # disabling the feature for the whole process run (wh-console-probe-degrade).
        if self._spawned_once:
            if self._restart_allowed():
                self._restart_count += 1
            elif not self._enter_or_recover_degraded_locked():
                return None
        self._spawned_once = True
        try:
            spawned = self._proc_factory()
        except Exception:
            # During a degraded episode a recovery spawn is EXPECTED to keep
            # failing until the terminal un-wedges; the once-per-episode
            # exhaustion ERROR already fired in _enter_or_recover_degraded_locked.
            # A raising factory (e.g. OSError creating the subprocess on a loaded
            # machine) must therefore log at DEBUG here, not ERROR, so it does not
            # re-fire a Windows notification at each backoff step (5s/10s/20s/60s)
            # -- the exact repeated-notification amplifier this change removes
            # (wh-console-probe-degrade.3.1). Outside a degraded episode the
            # spawn-failure count is bounded by the restart budget, so ERROR is
            # still appropriate there.
            if self._degraded_since is not None:
                logger.debug(
                    "console_probe_client: recovery spawn failed during degraded "
                    "episode",
                    exc_info=True,
                )
            else:
                logger.exception("console_probe_client: failed to spawn helper")
            self._proc = None
            return None
        # A freshly-spawned helper that is already dead (e.g. the factory
        # handed back a non-running process) must not be used. Treat it as a
        # spawn failure (return None) so the caller raises ConsoleProbeError.
        if not self._proc_alive(spawned):
            if spawned is not None:
                self._discard_proc(spawned)
            self._proc = None
            return None
        self._proc = spawned
        return self._proc

    @staticmethod
    def _discard_proc(proc):
        """Best-effort teardown of a dead/replaced helper process.

        Terminates the helper synchronously, then hands the BLOCKING part of the
        teardown (reap + kill-escalate + pipe close) to a detached daemon thread
        and returns immediately. Returns that thread (tests join it; production
        ignores it).

        Why the blocking part must run off the caller's lock (wh-console-probe-degrade.1.2):
        ``_discard_proc`` runs under ``self._lock`` on the read-timeout recycle
        path, and the helper timed out precisely because it is wedged in console
        kernel calls -- exactly the case where the process may not exit promptly
        after ``terminate``. If the reap ran inline it could hold the lock for up
        to two ``_DISCARD_WAIT_S`` waits, serialising the single-worker detector
        during the busy spell this fix targets. Worse, if the helper cannot be
        stopped at all, ``stdout.close()`` blocks on the reader thread's buffered
        read lock, which under ``self._lock`` would re-create the permanent stall
        this whole change removes. Running it in a daemon thread means a truly
        unkillable helper leaks only that one detached thread, never the probe
        path.

        Terminate stays synchronous (and non-blocking: it only signals the OS)
        so the helper's death closes ITS write end, delivering EOF to any
        ``_blocking_readline_with_timeout`` reader thread still blocked in
        ``readline`` -- closing our read end does NOT reliably wake a C-level
        blocked read on Windows, which is why terminate-before-close matters.
        """
        # Synchronous, non-blocking: signal the helper to die so its write end
        # closes (EOF to any blocked reader thread). Skip if already dead.
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        reaper = threading.Thread(
            target=ConsoleProbeClient._reap_and_close_proc,
            args=(proc,),
            name="console-probe-reaper",
            daemon=True,
        )
        reaper.start()
        return reaper

    @staticmethod
    def _reap_and_close_proc(proc) -> None:
        """Reap a terminated helper and close our pipe ends. Runs in a detached
        daemon thread (see ``_discard_proc``), so a wedged helper's ``wait`` or
        pipe ``close`` never blocks the caller's lock.
        """
        # Reap; escalate to kill if the helper ignores terminate within the
        # bound. (On Windows kill() and terminate() are both TerminateProcess,
        # so kill adds no extra force there but does on POSIX; either way the
        # second wait reaps whatever terminated.)
        try:
            proc.wait(timeout=_DISCARD_WAIT_S)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=_DISCARD_WAIT_S)
            except Exception:
                pass
        # Close our pipe ends. If the helper is truly unkillable the reader is
        # still blocked and this close blocks with it -- but only THIS detached
        # thread, never the probe path.
        for closer in ("stdin", "stdout"):
            stream = getattr(proc, closer, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    # --- public API ---------------------------------------------------------

    def is_at_prompt(self, terminal_process_name: str, terminal_pid: int) -> bool:
        """Return True iff the helper reports the terminal is at a prompt.

        Caches per pid for ``_CACHE_TTL`` seconds. A returned bool is a genuine
        answer from a validated round-trip (``True`` = at a prompt, ``False`` =
        shell is busy). Any transport failure (timeout, EOF, broken pipe,
        malformed JSON, pid mismatch, dead helper) raises ``ConsoleProbeError``
        instead of returning a bool, so the caller can distinguish a transport
        stall from a real busy verdict (wh-jvrs.3.1). A transport failure is NOT
        cached -- the next call re-probes rather than stranding the utterance on
        a stale failure.
        """
        now = time.monotonic()
        cached = self._cache.get(terminal_pid)
        if cached is not None and (now - cached[1]) < _CACHE_TTL:
            return cached[0]

        result = self._probe(terminal_pid)
        self._cache[terminal_pid] = (result, time.monotonic())
        return result

    def _probe(self, terminal_pid: int) -> bool:
        """Run one request/response round-trip and return the at-prompt bool.

        Returns the genuine answer (``True`` at a prompt, ``False`` busy) only
        from a complete, validated round-trip. Every transport failure -- no
        spawnable helper, a dead pipe, a read timeout, EOF/IOError, a malformed
        response, or a response-pid mismatch -- raises ``ConsoleProbeError``
        after recycling the helper, so the caller never sees a transport stall
        as a ``False`` busy verdict (wh-jvrs.3.1).
        """
        with self._lock:
            proc = self._ensure_proc()
            if proc is None:
                raise ConsoleProbeError("helper unavailable (spawn failed or budget exhausted)")
            stdin = getattr(proc, "stdin", None)
            stdout = getattr(proc, "stdout", None)
            if stdin is None or stdout is None:
                # A live-but-unusable helper (poll() is None yet a pipe is
                # missing) must be recycled, not left stored: _ensure_proc
                # only checks poll(), so the next probe would re-fetch this
                # same bad proc and raise forever, permanently wedging the
                # at-prompt detector. Recycle so the next call respawns a
                # clean helper, matching every other transport-failure branch
                # below (wh-jvrs.3.3).
                logger.debug(
                    "console_probe_client: helper missing stdin/stdout pipe; "
                    "recycling helper"
                )
                self._kill_proc_locked()
                raise ConsoleProbeError("helper has no stdin/stdout pipe")

            request = (json.dumps({"pid": terminal_pid}) + "\n").encode("utf-8")
            try:
                stdin.write(request)
                stdin.flush()
            except Exception as exc:
                logger.debug("console_probe_client: write failed; recycling helper")
                self._kill_proc_locked()
                raise ConsoleProbeError("write to helper failed") from exc

            try:
                raw = self._read_line(stdout, self._read_timeout_s)
            except TimeoutError as exc:
                logger.debug(
                    "console_probe_client: helper read timed out for pid=%d",
                    terminal_pid,
                )
                # A timed-out helper may still be alive but wedged; recycle it so
                # the next call gets a clean process.
                self._kill_proc_locked()
                raise ConsoleProbeError("helper read timed out") from exc
            except (EOFError, OSError) as exc:
                logger.debug(
                    "console_probe_client: helper EOF/IOError for pid=%d",
                    terminal_pid,
                )
                self._kill_proc_locked()
                raise ConsoleProbeError("helper EOF/IOError") from exc
            except Exception as exc:
                logger.debug("console_probe_client: unexpected read error", exc_info=True)
                self._kill_proc_locked()
                raise ConsoleProbeError("unexpected helper read error") from exc

            try:
                text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                payload = json.loads(text.strip())
                # Validate the echoed pid against the request pid (wh-jvrs.1.1.3).
                # The exchange is serialized so a mismatch should be impossible
                # today, but treating it as a transport fault (recycle + raise)
                # preserves the one-request-one-response invariant if the helper
                # protocol ever changes -- a stale line must never be read as the
                # current pid's answer and redirect into the wrong shell.
                resp_pid = int(payload["pid"])
                if resp_pid != terminal_pid:
                    logger.debug(
                        "console_probe_client: response pid %d != request pid "
                        "%d; recycling helper and raising",
                        resp_pid, terminal_pid,
                    )
                    self._kill_proc_locked()
                    raise ConsoleProbeError("response pid mismatch")
                # The result must be a genuine bool from a complete validated
                # round-trip. A bare bool() coercion would accept a malformed
                # value -- the string "false" or a numeric 1 are both truthy --
                # and hand the focus-redirect path a fabricated at-prompt=True
                # answer on a corrupted transport response. Treating anything
                # that is not exactly a bool as a transport fault (recycle +
                # raise) keeps the contract that a returned bool means ONLY
                # "validated True at-prompt / validated False busy"; everything
                # else is a ConsoleProbeError the policy routes to
                # prompt_detector_error (wh-jvrs.3.5). isinstance(..., bool) is
                # exact here: bool is an int subclass, so a numeric 1/0 (type
                # int) is correctly rejected.
                raw_result = payload["result"]
                if not isinstance(raw_result, bool):
                    logger.debug(
                        "console_probe_client: non-bool result %r for pid=%d; "
                        "recycling helper and raising",
                        raw_result, terminal_pid,
                    )
                    self._kill_proc_locked()
                    raise ConsoleProbeError("non-bool helper result")
                result = raw_result
                # A full, valid request/response round-trip proves the helper
                # is healthy: clear the respawn budget AND any degraded episode
                # so the budget bounds CONSECUTIVE failures (the real tight-loop
                # case the docstrings describe), not lifetime failures spread
                # across a long session. Without this reset, 5 transient
                # timeouts/EOFs over an arbitrarily long run -- each fully
                # recovered with many good probes in between -- would
                # permanently degrade the at-prompt probe (wh-jvrs.2.1), and a
                # recovery spawn after the cooldown could not re-arm a fresh
                # episode later (wh-console-probe-degrade).
                self._reset_respawn_budget_locked()
                logger.debug(
                    "console_probe_client: pid=%d at_prompt=%s",
                    terminal_pid, result,
                )
                return result
            except ConsoleProbeError:
                # The pid-mismatch and non-bool-result raises above are already
                # transport faults with the helper recycled; propagate unchanged.
                raise
            except Exception as exc:
                # A malformed/short response is a transport fault, not a busy
                # verdict: the pipe may be desynced, so recycle the helper and
                # raise rather than reusing a stream that could hand the next
                # request a stale line (wh-jvrs.3.1).
                logger.debug("console_probe_client: bad response JSON; recycling helper")
                self._kill_proc_locked()
                raise ConsoleProbeError("malformed helper response") from exc

    def _kill_proc_locked(self) -> None:
        """Tear down the current helper so the next probe respawns it.

        Caller must hold ``self._lock``.
        """
        proc = self._proc
        if proc is not None:
            self._discard_proc(proc)
            self._proc = None

    def close(self) -> None:
        """Terminate the helper subprocess and release its pipes.

        ``_discard_proc`` already terminates, reaps, and kill-escalates a
        stubborn helper, so this is just that teardown plus clearing the stored
        reference under the lock.
        """
        with self._lock:
            proc = self._proc
            if proc is None:
                return
            self._discard_proc(proc)
            self._proc = None
