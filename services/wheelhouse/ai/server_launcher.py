"""Start and stop a llama.cpp model server owned by Wheelhouse.

The AI subsystem is a thin client for an OpenAI-compatible server. When
``[ai.runtime] enabled`` is true, this class is what puts a server at the
address the client is already configured to use. When it is false, nothing
here runs and the client talks to whatever server the user started.

Why the reasoning flags are not optional
========================================
``--reasoning off --reasoning-budget 0`` must be on the command line. Without
them a reasoning-capable model can spend its entire answer budget on hidden
thinking and return HTTP 200 with empty content and
``finish_reason="length"``. ``AIService`` reports that as
``exhausted_reasoning``, which is an accurate message but one the user cannot
act on, and the correction silently does nothing. Turning reasoning off at the
server is the fix; it is not a tuning preference. Measured on 2026-07-26: with
reasoning left on, the 12B model returned an empty answer in 16 of 50 trials.

Two ways the server outlives us, and the answer to each
======================================================
Wheelhouse can exit without running its shutdown path: a crash, antivirus
ending the Logic process, a power loss. The server it started keeps running,
holding the port and several gigabytes of memory, and the next start cannot
bind. A user who controls this machine by voice cannot open Task Manager to
fix that.

Two mechanisms, and they are not alternatives:

- A Windows job object set to end its processes when it closes. Windows closes
  the job for us however the Logic process exits, so the server goes with us.
  This prevents the orphan.
- A file recording the server's process id. The next start reads it, and stops
  a server that is still running from last time. This recovers from an orphan
  that happened anyway -- the job object could not be created, or the machine
  lost power.

The record says who owns the server, not just which one it is
=============================================================
The file records the Wheelhouse process that started the server as well as the
server itself. Without that, a second Wheelhouse cannot tell an abandoned
server from one a running Wheelhouse started a moment ago, and the only safe
reading -- "still running, so stop it" -- ends a server that is in active use.
An orphan is precisely a server whose owner is gone, so the owner is what has
to be recorded.

Wheelhouse does not enforce a single instance (``launcher.py`` reads its own
process-id file and does nothing with it), so two of them starting at once is
a real sequence of events, not a theoretical one.

One start at a time
===================
Clearing an abandoned server, starting the new one, and recording it have to
happen as one step. Two starts interleaved inside it can both find no record
and both start a server, after which the one that records itself second erases
the other's record and that server is the next orphan. A named mutex, held
across those three actions, is what makes them one step. It is not held across
the wait for the server to become ready: that part is slow and touches nothing
another start could read.

Nothing here refuses to start because a lock could not be taken -- a failure
to create the mutex logs and proceeds, matching the rest of this module, where
every recovery mechanism is best-effort and the AI features are optional.

Testability
===========
The process starter, the health probe, the sleep, the clock, the lock, the
owner check, and both halves of the stale-server check are injected. That is
what lets the whole state machine -- the startup timeout, the
terminate-then-kill path, the crash-recovery path, and two launchers racing --
be tested without starting a real server or waiting real seconds.
"""

from __future__ import annotations

import contextlib
import enum
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, ContextManager, Iterator, NamedTuple, Optional, Protocol

import psutil

from ai.runtime_config import RuntimeConfig

logger = logging.getLogger(__name__)


class Liveness(enum.Enum):
    """What looking at a process id actually established.

    The third answer is the point. Windows refuses to tell us about some
    processes, and every such refusal used to be read as "gone" -- the one
    reading that licenses the destructive action. Answering "gone" for a
    process we could not inspect makes the recovery path delete the only
    record of a server that may still be running, and makes the owner check
    terminate a server another running Wheelhouse is using.

    GONE means established absence: the process id belongs to nothing, or it
    belongs to a program that is positively not the one we asked about.
    UNKNOWN means the question was not answered, and nothing may be destroyed
    on the strength of it.
    """

    ALIVE = "alive"
    GONE = "gone"
    UNKNOWN = "unknown"


class ServerProcess(Protocol):
    """The part of a Popen handle this class uses.

    Declared so a test can inject a stand-in without subclassing Popen, and so
    the type checker knows the handle has these members.
    """

    pid: int

    def poll(self) -> Optional[int]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: Optional[float] = None) -> int: ...

BINARY_NAME = "llama-server.exe"

# The name of the file that records the running server's process id, written
# in the user data directory beside the other files Wheelhouse keeps there.
PID_FILE_NAME = "llama-server.pid"

# How often to ask the server whether it is ready.
_POLL_INTERVAL_S = 0.5

# How long to wait for a terminate to take effect before killing.
_TERMINATE_GRACE_S = 5.0

# How long a single health request may take. Short: the server either answers
# promptly or is not up yet.
_HEALTH_TIMEOUT_S = 2.0

# The mutex that lets only one start at a time run the clear/start/record step.
# Per port, so two runtimes configured on different ports do not wait for each
# other. "Local\" scopes it to the logon session, which is the right scope for
# a desktop application: two users signed in at once each get their own.
_START_LOCK_NAME = "Local\\WheelhouseLocalAIServerStart_{port}"

# The step this guards is short -- it starts a process and writes one small
# file, and does not wait for the model to load. A wait this long therefore
# means the holder died in a way that left the mutex held, or something is
# badly wrong; proceeding is better than refusing to start the AI features.
_START_LOCK_TIMEOUT_MS = 30_000


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        # Every pointer-sized field is c_size_t rather than a 32-bit type. A
        # 32-bit field here silently shortens the structure and the call fails
        # or corrupts memory on 64-bit Windows.
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _kernel32():
        """kernel32 with the return types declared.

        Without an explicit restype ctypes assumes a 32-bit int, which cuts
        the top half off every handle these calls return.
        """
        dll = ctypes.windll.kernel32
        dll.CreateJobObjectW.restype = wintypes.HANDLE
        dll.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        dll.SetInformationJobObject.restype = wintypes.BOOL
        dll.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        dll.OpenProcess.restype = wintypes.HANDLE
        dll.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ]
        dll.AssignProcessToJobObject.restype = wintypes.BOOL
        dll.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        dll.CloseHandle.restype = wintypes.BOOL
        dll.CloseHandle.argtypes = [wintypes.HANDLE]
        dll.CreateMutexW.restype = wintypes.HANDLE
        dll.CreateMutexW.argtypes = [
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        dll.WaitForSingleObject.restype = wintypes.DWORD
        dll.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        dll.ReleaseMutex.restype = wintypes.BOOL
        dll.ReleaseMutex.argtypes = [wintypes.HANDLE]
        return dll


class _JobHandle:
    """Holds a job object open.

    Closing the last handle to the job ends every process inside it, which is
    the whole mechanism: Windows does that for us when this process exits,
    however it exits. Anything that drops this object therefore stops the
    server, so it must be held for as long as the server is wanted.
    """

    def __init__(self, handle: int):
        self._handle: Optional[int] = handle

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            try:
                _kernel32().CloseHandle(handle)
            except Exception:  # noqa: BLE001 -- closing must not raise
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 -- runs during garbage collection
            pass


def _kill_child_when_we_exit(process: ServerProcess) -> Optional[_JobHandle]:
    """Put the server in a job object that ends when we do.

    Returns the handle to hold, or None when the job could not be set up. A
    failure here is not fatal: the recorded process id still lets the next
    start find and stop an orphaned server.
    """
    if sys.platform != "win32":
        return None

    job = None
    process_handle = None
    try:
        dll = _kernel32()
        job = dll.CreateJobObjectW(None, None)
        if not job:
            logger.warning("Could not create a job object for the local AI server.")
            return None

        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not dll.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            logger.warning(
                "Could not set the job object to end the local AI server with us."
            )
            return None

        process_handle = dll.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, process.pid
        )
        if not process_handle:
            logger.warning("Could not open the local AI server to put it in a job.")
            return None

        if not dll.AssignProcessToJobObject(job, process_handle):
            logger.warning("Could not put the local AI server in its job object.")
            return None

        handle, job = job, None  # handed to _JobHandle; do not close it here
        return _JobHandle(handle)
    except Exception as exc:  # noqa: BLE001 -- startup must not raise
        logger.warning("Could not tie the local AI server's lifetime to ours: %s", exc)
        return None
    finally:
        for leftover in (process_handle, job):
            if leftover:
                try:
                    _kernel32().CloseHandle(leftover)
                except Exception:  # noqa: BLE001
                    pass


def _default_spawn(command: list[str]) -> ServerProcess:
    """Start the server detached, matching how STT providers are started."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        ),
        start_new_session=sys.platform != "win32",
    )
    job = _kill_child_when_we_exit(process)
    if job is not None:
        # Attaching the handle to the process object is what keeps it alive.
        # Dropping it would close the job, which would stop the server we just
        # started.
        process._wheelhouse_job = job  # type: ignore[attr-defined]
    else:
        logger.warning(
            "The local AI server is not tied to this process; if Wheelhouse "
            "stops without shutting down, the recorded process id is what "
            "will find it."
        )
    return process


def _default_stale_probe(pid: int) -> Liveness:
    """Whether that number is a model server of ours, still running.

    The name check is not a nicety. Windows gives process ids out again once a
    program ends, so a number recorded by a session that crashed hours ago may
    now belong to a program the user is relying on. Terminating on the number
    alone would end that program.

    A name that does not match is GONE: the server that number named has
    ended, and something else has the number now. A name we could not read at
    all is UNKNOWN, because deleting the record on that basis would abandon a
    server that may still be holding the port.
    """
    try:
        name = psutil.Process(pid).name()
    except psutil.NoSuchProcess:
        return Liveness.GONE
    except Exception as exc:  # noqa: BLE001 -- protected or unreadable
        logger.warning(
            "Could not read what process %d is (%s), so it is not treated as "
            "a server that has gone.", pid, exc,
        )
        return Liveness.UNKNOWN
    return Liveness.ALIVE if name.lower() == BINARY_NAME.lower() else Liveness.GONE


def _default_stale_terminate(pid: int) -> bool:
    """Stop a model server left behind by an earlier session.

    Returns True only when the process is confirmed gone. Reporting success
    for a server that is still running is worse than reporting failure: the
    caller deletes the record on success, and that record is the only way
    anything can find the server again. It would then hold the port and its
    several gigabytes of memory for as long as the machine stays on, with no
    later start able to do anything about it.
    """
    try:
        process = psutil.Process(pid)
        process.terminate()
        try:
            process.wait(timeout=_TERMINATE_GRACE_S)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=_TERMINATE_GRACE_S)
        logger.info(
            "Stopped a local AI server left running by an earlier session "
            "(process %d).", pid,
        )
        return True
    except psutil.NoSuchProcess:
        # It ended between the check and the terminate. That is the outcome
        # we wanted, not a failure.
        return True
    except Exception as exc:  # noqa: BLE001 -- startup must not raise
        logger.warning(
            "Could not stop the local AI server left at process %d: %s", pid, exc
        )
        return False


def _own_process_name() -> str:
    """This process's executable name, recorded so a later start can check it.

    An empty name must not be recorded. The next start reads a record with no
    owner name as an owner that has gone, and then stops a server that is in
    active use -- so a failure to read our own name here would cost another
    Wheelhouse its server.

    ``sys.executable`` is the fallback because it cannot fail and names the
    same program psutil would: in a source checkout both say ``python.exe``,
    and in a frozen build both say the Wheelhouse executable. psutil is still
    asked first, since it is what the next start compares against.
    """
    try:
        name = psutil.Process(os.getpid()).name()
        if name:
            return name
    except Exception:  # noqa: BLE001 -- fall back rather than fail a start
        pass
    return os.path.basename(sys.executable)


def _default_owner_alive(pid: int, name: str) -> Liveness:
    """Whether the Wheelhouse that started a server is still running.

    This is what separates an abandoned server from one in active use. The
    recorded name is checked as well as the number because Windows issues
    process ids again after a process ends, and a number recorded hours ago
    may now belong to something else entirely -- which would make an orphan
    look owned, and leave it running forever.

    An owner we could not inspect is UNKNOWN, never GONE. Reading it as GONE
    would terminate a server whose owner is alive and using it.
    """
    if not name:
        # A record written by a build that recorded no owner. Reading it as an
        # owner that has gone is what that build did, and it is the only
        # reading available: there is nothing to compare a process against.
        return Liveness.GONE
    try:
        actual = psutil.Process(pid).name()
    except psutil.NoSuchProcess:
        return Liveness.GONE
    except Exception as exc:  # noqa: BLE001 -- protected or unreadable
        logger.warning(
            "Could not read what process %d is (%s), so the Wheelhouse that "
            "owns the server is not treated as gone.", pid, exc,
        )
        return Liveness.UNKNOWN
    return Liveness.ALIVE if actual.lower() == name.lower() else Liveness.GONE


@contextlib.contextmanager
def _default_start_lock(port: int) -> Iterator[bool]:
    """Hold the start mutex for the block, if one can be had.

    Yields either way, and yields whether the lock was actually taken. The
    block decides what to do without it: starting anyway leaves the
    pre-existing race between two simultaneous starts, which is rare and
    recoverable, while refusing to start would turn a lock problem into no AI
    features at all. Deleting a record is the one thing that is not
    recoverable, so that is the one caller that reads the value and stops.
    """
    if sys.platform != "win32":
        # The server is a Windows executable, so this branch does not run in
        # a shipped build. Named mutexes have no portable equivalent, and
        # inventing a lock file here would add a stale-lock problem to a
        # platform that cannot reach this code. Nothing else runs here either,
        # so there is nothing to be excluded from.
        yield True
        return

    handle = None
    acquired = False
    try:
        dll = _kernel32()
        handle = dll.CreateMutexW(None, False, _START_LOCK_NAME.format(port=port))
        if not handle:
            logger.warning(
                "Could not create the lock that keeps two local AI server "
                "starts apart; starting without it."
            )
        else:
            # 0 is granted; 0x80 is granted after the previous holder ended
            # without releasing, which is exactly the crash this module
            # exists to survive, and it grants ownership just the same.
            result = dll.WaitForSingleObject(handle, _START_LOCK_TIMEOUT_MS)
            if result in (0x0, 0x80):
                acquired = True
            else:
                logger.warning(
                    "Waited %d seconds for another local AI server start to "
                    "finish and it did not; starting anyway.",
                    _START_LOCK_TIMEOUT_MS // 1000,
                )
    except Exception as exc:  # noqa: BLE001 -- startup must not raise
        logger.warning("Could not lock the local AI server start: %s", exc)

    try:
        yield acquired
    finally:
        try:
            if handle:
                if acquired:
                    _kernel32().ReleaseMutex(handle)
                _kernel32().CloseHandle(handle)
        except Exception:  # noqa: BLE001 -- releasing must not raise
            pass


class _ServerRecord(NamedTuple):
    """What the record file says: which server, and whose it is."""

    server_pid: int
    owner_pid: int
    owner_name: str

    def format(self) -> str:
        return (
            f"server_pid={self.server_pid}\n"
            f"owner_pid={self.owner_pid}\n"
            f"owner_name={self.owner_name}\n"
        )

    @classmethod
    def parse(cls, raw: str) -> Optional["_ServerRecord"]:
        """Read a record, or None when the text is not one.

        A file holding only a number is a record written by an earlier build.
        It is read as a server with no known owner, which makes the owner
        look gone -- the behaviour that build had, and the only reading
        available for a record that cannot say otherwise.

        A structured record is held to all three fields. This build writes
        them together in one move, so a structured record missing any of them
        was written by something interrupted, and is corrupt rather than
        incomplete. Filling in a missing owner would produce exactly the
        record the paragraph above reads as abandoned, and the next start
        would stop a server that is in use.
        """
        text = raw.strip()
        if not text:
            return None
        if text.isdigit():
            return cls(server_pid=int(text), owner_pid=0, owner_name="")

        fields = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
        try:
            server_pid = int(fields["server_pid"])
            owner_pid = int(fields["owner_pid"])
            owner_name = fields["owner_name"]
        except (KeyError, ValueError):
            return None
        if not owner_name:
            return None
        return cls(
            server_pid=server_pid,
            owner_pid=owner_pid,
            owner_name=owner_name,
        )


def _default_probe(port: int) -> bool:
    """True when the server reports itself ready."""
    try:
        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT_S) as response:
            return json.loads(response.read()).get("status") == "ok"
    except Exception:
        # Connection refused while the server is still loading the model is the
        # normal case here, not an error worth logging on every poll.
        return False


class LocalAIServerLauncher:
    """Owns the lifetime of the Wheelhouse-managed model server."""

    def __init__(
        self,
        config: RuntimeConfig,
        spawn: Optional[Callable[[list[str]], ServerProcess]] = None,
        probe: Optional[Callable[[int], bool]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        pid_file: Optional[Path] = None,
        stale_probe: Optional[Callable[[int], Liveness]] = None,
        stale_terminate: Optional[Callable[[int], bool]] = None,
        owner_alive: Optional[Callable[[int, str], Liveness]] = None,
        start_lock: Optional[Callable[[int], ContextManager[bool]]] = None,
    ):
        self._config = config
        self._spawn = spawn or _default_spawn
        self._probe = probe or _default_probe
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._pid_file = pid_file
        self._stale_probe = stale_probe or _default_stale_probe
        self._stale_terminate = stale_terminate or _default_stale_terminate
        self._owner_alive = owner_alive or _default_owner_alive
        self._start_lock = start_lock or _default_start_lock
        self._process: Optional[ServerProcess] = None
        # A server a stop asked to end and could not confirm the exit of. Kept
        # apart from _process so that is_running keeps saying no -- readiness
        # recovery stops the old server and starts a new one, and a launcher
        # that answered "already running" there would do nothing and report
        # success. Kept at all so the next stop, and shutdown above all, has
        # something to try again on.
        self._unstopped: Optional[ServerProcess] = None
        # Exactly the text we last wrote to the record file, or None when no
        # record of ours is on disk. Kept rather than rebuilt, because the
        # delete compares before removing and a rebuilt record could differ
        # from the one written -- and would then fail to remove our own file.
        # It is also what lets a start clean up after a shutdown that took the
        # process from under it, when there is no longer a process to rebuild
        # a record from.
        self._record_text: Optional[str] = None
        # Set once, by shutdown(), and never cleared: this launcher is done.
        # Distinct from stop(), which start() itself calls when a server never
        # became healthy -- that one must leave a later attempt possible.
        self._shutdown = False

    @property
    def is_running(self) -> bool:
        """Whether a server we started is still running.

        This asks the process, rather than remembering that we once started
        one. A server that has exited on its own -- out of memory, ended by
        the user, removed by antivirus -- would otherwise be reported as
        running for the rest of the session, and start() would decline to
        replace it.
        """
        process = self._process
        if process is None:
            return False
        exit_code = process.poll()
        if exit_code is None:
            return True
        logger.warning(
            "The local AI server exited on its own with code %s.", exit_code
        )
        self._process = None
        return False

    def build_command(self) -> list[str]:
        """The exact command line, so a test can assert it without spawning."""
        cfg = self._config
        return [
            str(Path(cfg.binary_dir) / BINARY_NAME),
            "-m", cfg.model_path,
            "--host", "127.0.0.1",
            "--port", str(cfg.port),
            "-c", str(cfg.context_size),
            "-ngl", str(cfg.gpu_layers),
            "--no-webui",
            # Not optional -- see the module docstring.
            "--reasoning", "off",
            "--reasoning-budget", "0",
        ]

    def start(self) -> bool:
        """Start the server and wait for it to report healthy.

        Returns True when a server is running and healthy, which includes the
        case where one was already started. Returns False on any failure; the
        caller leaves the AI features off rather than blocking startup.
        """
        if self._shutdown:
            return False
        if self.is_running:
            return True
        if not self._config.enabled:
            return False

        # Clearing an abandoned server, starting the new one and recording it
        # are one step, so that a second start cannot land in the middle and
        # leave two servers with one record between them. The wait for the
        # model to load is deliberately outside: it is slow, and it reads
        # nothing another start could be writing.
        with self._start_lock(self._config.port):
            if not self._clear_unstopped_server():
                return False
            if not self._clear_stale_server():
                return False
            if not self._prepare_record_location():
                return False

            command = self.build_command()
            logger.info("Starting the local AI server on port %d", self._config.port)
            try:
                self._process = self._spawn(command)
            except Exception as exc:  # noqa: BLE001 -- startup must not raise
                logger.error("Could not start the local AI server: %s", exc)
                self._process = None
                return False

            if not self._write_pid_file():
                # A server nothing can find again is worse than no server. If
                # Wheelhouse now stops without shutting down -- and the job
                # object is the only other thing that would end it -- this one
                # holds the port and its several gigabytes with no record for
                # any later start to act on.
                logger.error(
                    "The local AI server was started but could not be "
                    "recorded. Stopping it and leaving the AI features off."
                )
                self.stop()
                return False

        if self._shutdown:
            # Shutdown ran while we were spawning. Its own stop() looked for a
            # server before there was one to find, so this is the only place
            # that can stop what we have just started.
            logger.info(
                "Wheelhouse is shutting down; stopping the local AI server "
                "this start had already begun."
            )
            self.stop()
            # stop() returns at once when a shutdown already took the process,
            # and a shutdown that landed while the record was being written
            # found no record to remove. Between them the record survives,
            # naming a server that has been stopped -- so remove it here,
            # where the text we wrote is still known.
            self._remove_own_record()
            return False

        if self._wait_for_health():
            logger.info("The local AI server is ready on port %d", self._config.port)
            return True

        # Never leave a server we started running unattended. It may still come
        # up after we gave up waiting, and then nothing owns it.
        logger.error(
            "The local AI server did not become ready within %d seconds; "
            "stopping it and leaving the AI features off.",
            self._config.startup_timeout_seconds,
        )
        self.stop()
        return False

    def restart(self) -> bool:
        """Replace our server, whether or not its process is still running.

        start() is idempotent on purpose: it returns True the moment it sees
        that the process it started has not exited, without asking whether
        that process answers. That is the right answer for a caller who only
        wants a server to exist, and it is the wrong one for the readiness
        supervisor, which asks for this only after watching the server stop
        answering. A server can stop answering while its process stays up --
        a deadlocked llama server, a health thread that died, a model load
        that never finished -- and for those, start() reports success, leaves
        the same server in place, and gets asked again a minute later for as
        long as Wheelhouse runs.

        Stopping first is what makes this a replacement. It also keeps the
        one safety property that matters here: stop() holds on to the handle
        when it cannot confirm the exit, and the start below refuses to spawn
        while that handle is held, so a server that will not die does not
        become two servers with one record between them.

        There is deliberately no shutdown check of its own. start() already
        refuses once shutdown has run, so one here would change nothing about
        what this returns or whether a server is spawned -- all it would do is
        skip the stop, and during a shutdown a stop is the one thing worth
        keeping: it is another attempt at a server an earlier one could not
        confirm the exit of.
        """
        self.stop()
        return self.start()

    def _wait_for_health(self) -> bool:
        deadline = self._monotonic() + self._config.startup_timeout_seconds
        while self._monotonic() < deadline:
            if self._shutdown:
                # Shutdown has stopped this server already. Without this the
                # thread spends the rest of the startup timeout -- ninety
                # seconds by default -- asking a stopped server whether it is
                # ready, long after everything else has gone.
                return False
            # A process that has already exited will never become healthy, so
            # do not spend the rest of the timeout asking.
            if self._process is not None and self._process.poll() is not None:
                logger.error(
                    "The local AI server exited during startup with code %s",
                    self._process.poll(),
                )
                return False
            if self._probe(self._config.port):
                return True
            self._sleep(_POLL_INTERVAL_S)
        return False

    def _port_is_free(self) -> bool:
        """Whether nothing is already answering on the configured port.

        Asked on the two paths where nothing of ours is recorded on the port:
        no record at all, and a record whose server is confirmed gone. A
        server started outside Wheelhouse answers there just the same, and it
        is the same probe that later decides our own start succeeded -- so
        without this check a start spawns a child that cannot bind, watches
        the incumbent answer for it, reports success, and records a process
        that is about to exit.
        """
        if not self._probe(self._config.port):
            return True
        logger.error(
            "Something is already answering on port %d and no record of ours "
            "says what it is, so no local AI server is started here.",
            self._config.port,
        )
        return False

    def _clear_stale_server(self) -> bool:
        """Make the port free to bind, and say whether it now is.

        Returns True when the start may go ahead. False means something is
        still on the port that we must not or could not remove; the caller
        leaves the AI features off rather than starting a server that cannot
        bind, and the record is left in place so a later start can try again.

        Reading the file is the only way to find an abandoned server: it
        belongs to no process tree of ours any more.
        """
        path = self._pid_file
        if path is None:
            # No record was asked for, so there is nothing to recover from and
            # nothing that could be orphaned. Only the tests reach this: every
            # launcher Wheelhouse builds is given a record path.
            return True
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # No server has been recorded. The ordinary first start -- but
            # "nothing recorded" is not "nothing running": a server the user
            # started by hand, or one whose record was lost, holds the port
            # just the same.
            return self._port_is_free()
        except OSError as exc:
            # Unreadable is not absent. The file may name a server that is
            # running, and a second server on the same port cannot bind --
            # after which the first is still there and still unfindable.
            logger.error(
                "Could not read the record of the local AI server at %s (%s). "
                "It may name a server that is still running, so no server is "
                "started here.", path, exc,
            )
            return False

        record = _ServerRecord.parse(raw)
        if record is None:
            # Half-written, or written by something that did not finish. The
            # port is the only thing left that can say whether a server is
            # there, and it answers for any server, not only ours.
            if self._probe(self._config.port):
                logger.error(
                    "The record of the local AI server at %s does not say "
                    "which process it is (%r), and a server is answering on "
                    "port %d. It is left alone and no server is started here.",
                    path, raw.strip(), self._config.port,
                )
                return False
            logger.warning(
                "The record of the local AI server at %s does not say which "
                "process it is (%r), and nothing is answering on port %d; "
                "ignoring it.", path, raw.strip(), self._config.port,
            )
            self._remove_pid_file(raw)
            return True

        server = self._stale_probe(record.server_pid)
        if server is Liveness.UNKNOWN:
            # The recorded process refuses to say what it is. It may well be
            # the server on the port, and we cannot show that it is not.
            if self._probe(self._config.port):
                logger.error(
                    "Could not tell what process %d is, and a server is "
                    "answering on port %d. It is left alone and no server is "
                    "started here.", record.server_pid, self._config.port,
                )
                return False
            # Nothing usable is on the port. Refusing on the strength of an
            # inspection that will fail the same way every time would leave
            # the AI features off for good, so the start goes ahead; the
            # record is left for _write_pid_file to replace with ours.
            logger.warning(
                "Could not tell what process %d is, but nothing is answering "
                "on port %d, so a new local AI server is started.",
                record.server_pid, self._config.port,
            )
            return True

        if server is not Liveness.ALIVE:
            # The number names nothing, or names a program that is not one of
            # our servers -- the process id was issued again after the server
            # ended. Either way there is nothing of ours to stop, so the
            # record goes. Whether the port is free is a separate question:
            # the recorded server really is dead, and something else can be
            # answering there.
            self._remove_pid_file(raw)
            return self._port_is_free()

        if record.owner_pid == os.getpid():
            # Our own record, for a server this session started and has since
            # lost hold of -- a stop that could not confirm the exit leaves
            # exactly this. The owner check below would find that process
            # alive, because it is us, and read it as another Wheelhouse; one
            # failed stop would then refuse every start for the rest of the
            # session.
            logger.warning(
                "The local AI server at process %d was started by this "
                "Wheelhouse and is still running; stopping it before starting "
                "a new one.", record.server_pid,
            )
        else:
            owner = self._owner_alive(record.owner_pid, record.owner_name)
            if owner is Liveness.ALIVE:
                # Another Wheelhouse started this server and is still running.
                # Stopping it would take the AI features away from a session
                # that is using them, so this start gives up instead.
                logger.warning(
                    "Another Wheelhouse (process %d) is already running the "
                    "local AI server on port %d; leaving it alone and starting "
                    "no server here.", record.owner_pid, self._config.port,
                )
                return False

            if owner is Liveness.UNKNOWN:
                # The server is confirmed one of ours; only whose it is is in
                # doubt. Reading that as abandoned terminates a server another
                # session may be using, and unlike the cases above there is no
                # port answer to fall back on -- a server answering on the port
                # is exactly what we would be ending.
                logger.error(
                    "Could not tell whether the Wheelhouse at process %d that "
                    "started the local AI server is still running, so its "
                    "server is left alone and no server is started here.",
                    record.owner_pid,
                )
                return False

            logger.warning(
                "A local AI server from an earlier session is still running "
                "(process %d); stopping it before starting a new one.",
                record.server_pid,
            )
        if not self._stale_terminate(record.server_pid):
            # The record is the only way anything can find that server, so it
            # stays. Deleting it here and starting a replacement would leave
            # the old one holding the port and its memory permanently, with
            # every later start unable to see it.
            logger.error(
                "Could not stop the local AI server left at process %d. It "
                "still holds port %d, so no new server is started. Its "
                "process id is kept at %s so the next start can try again.",
                record.server_pid, self._config.port, path,
            )
            return False

        self._remove_pid_file(raw)
        return True

    @staticmethod
    def _own_record(process: ServerProcess) -> _ServerRecord:
        """The record this launcher writes for a server it started."""
        return _ServerRecord(
            server_pid=process.pid,
            owner_pid=os.getpid(),
            owner_name=_own_process_name(),
        )

    def _prepare_record_location(self) -> bool:
        """Make the directory the record goes in, before anything is spawned.

        Publication happens after the spawn, so a directory that cannot be
        created is otherwise discovered with a live server already running and
        nothing on disk naming it. Doing it first turns the commonest
        publication failure into an ordinary refusal to start.
        """
        path = self._pid_file
        if path is None:
            return True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error(
                "Could not create the directory the local AI server's record "
                "goes in (%s): %s. No server is started, because one that "
                "cannot be recorded cannot be found again.", path.parent, exc,
            )
            return False
        return True

    def _write_pid_file(self, process: Optional[ServerProcess] = None) -> bool:
        """Record the server and the Wheelhouse that owns it.

        Returns whether a record is on disk. False means the caller must not
        report a successful start.

        The process is named explicitly by the one caller that has already
        given the handle up: a stop that could not confirm the exit, writing a
        record for a server it is about to stop holding. Everyone else records
        the server this launcher is currently running.

        The record goes to a temporary sibling and is moved into place, since
        os.replace is atomic on Windows and on POSIX. Writing the final path
        directly truncates it first, so an interruption leaves a half-written
        record -- and a truncated record used to read as a server with no
        owner, which is precisely the reading that licenses the next start to
        terminate it.
        """
        path = self._pid_file
        process = process if process is not None else self._process
        if path is None or process is None:
            # No record was asked for. Nothing to publish, nothing to fail.
            return True

        record = self._own_record(process)
        if not record.owner_name:
            logger.error(
                "Could not establish this process's own name, so the local AI "
                "server cannot be recorded as ours. A record naming no owner "
                "would tell the next Wheelhouse this server is abandoned."
            )
            return False

        text = record.format()
        temporary = path.with_name(path.name + ".new")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
        except OSError as exc:
            logger.error(
                "Could not record the local AI server's process id at %s: %s.",
                path, exc,
            )
            with contextlib.suppress(OSError):
                temporary.unlink()
            return False

        self._record_text = text
        return True

    def _remove_own_record(self) -> None:
        """Remove the record this launcher wrote, if one is still on disk.

        Held under the lock the starts take. Comparing the file to what we
        wrote is a read and then an unlink, not one operation, so another
        Wheelhouse can publish its own record between the two -- after which
        this deletes a record naming a server that is running, and that server
        can never be found again. The comparison only means something while
        publication is excluded.

        Without the lock the deletion is skipped and _record_text is kept, so
        a later stop can try again. Leaving a record that names a stopped
        server costs the next start one inspection; deleting one that names a
        running server costs the user the port until the machine restarts.
        """
        record = self._record_text
        if record is None:
            return
        with self._start_lock(self._config.port) as exclusive:
            if not exclusive:
                logger.warning(
                    "The local AI server's record could not be removed safely "
                    "because another Wheelhouse may be starting one. It is "
                    "left at %s; the next start will clear it.", self._pid_file,
                )
                return
            self._remove_pid_file(record)
        self._record_text = None

    def _remove_pid_file(self, expected: str) -> None:
        """Delete the record, but only while it still says what we read.

        Comparing before deleting is what stops a start that is giving up
        from deleting the record another start has just written for a server
        that is running. Losing that record makes the running server
        unfindable, and it becomes the next orphan.
        """
        path = self._pid_file
        if path is None:
            return
        try:
            if path.read_text(encoding="utf-8") != expected:
                return
        except OSError:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def shutdown(self) -> None:
        """Stop the server for good: Wheelhouse itself is going.

        Separate from stop() because a start that gives up waiting for a
        server to become healthy calls stop() too, and that must leave a
        later attempt possible -- the next readiness check is entitled to try
        again. This one is final.

        The order matters. The flag is set before stopping anything, so that a
        start already running on another thread sees it. Set afterwards, a
        start could spawn a server in the window between the stop finding
        nothing and the flag appearing, and that server would outlive us with
        only the job object to end it.
        """
        self._shutdown = True
        self.stop()

    def _end_process(self, process: ServerProcess) -> bool:
        """Ask the process to stop, then insist. Whether its exit is confirmed.

        False is the reading that matters: it does not mean the server is
        still running for certain, only that nobody was able to watch it go.
        Every caller treats that as "still running", because the alternative
        is to forget a server that may hold the port and several gigabytes.
        """
        if process.poll() is None:
            logger.info("Stopping the local AI server")
            try:
                process.terminate()
                process.wait(timeout=_TERMINATE_GRACE_S)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "The local AI server did not stop when asked; killing it."
                )
                try:
                    process.kill()
                    process.wait(timeout=_TERMINATE_GRACE_S)
                except Exception as exc:  # noqa: BLE001 -- must not raise
                    logger.error("Could not kill the local AI server: %s", exc)
            except Exception as exc:  # noqa: BLE001 -- shutdown must not raise
                logger.error("Could not stop the local AI server: %s", exc)
        return process.poll() is not None

    def _clear_unstopped_server(self) -> bool:
        """Deal with a server an earlier stop could not confirm the exit of.

        Asked before the record is read, and it overrules the record. The
        record describes a process by number and can be absent, stale, or
        about someone else's server; a retained handle is this launcher's own
        knowledge that it started a server and nobody watched it stop. Reading
        the record instead can find nothing, see a silent port -- a server
        that never managed to bind leaves exactly that -- and start a second
        server. The next stop reads the newer handle first and drops the
        older one, and the first server is then running with no handle and no
        record naming it.

        Refusing when the retry fails does not repeat the lockout that the
        owner check used to cause. That one came from a record naming this
        process, which stayed on disk and refused every start for the rest of
        the session. Here the process is asked again on each attempt, and one
        that has since exited on its own frees the next start immediately.
        """
        process = self._unstopped
        if process is None:
            return True
        logger.warning(
            "A local AI server this Wheelhouse started could not be stopped "
            "earlier; trying again before starting another."
        )
        if not self._end_process(process):
            logger.error(
                "The local AI server at process %d still will not stop, so no "
                "second one is started here. Two servers of ours running with "
                "one record between them is how a server stops being findable.",
                process.pid,
            )
            return False
        self._unstopped = None
        return True

    def stop(self) -> None:
        """Stop the server, clearing its record once the exit is confirmed.

        The record used to be removed first, reasoning that one must never be
        left naming a server that has gone. That case is harmless: the next
        start looks at what the recorded process actually is, finds it is not
        one of our servers, and clears the record itself. The opposite case is
        not harmless. A server still running with no record naming it cannot
        be found by any later start, and holds the port and its several
        gigabytes until the machine is restarted -- which is exactly the
        failure the record exists to prevent.

        So every path that could not confirm the exit keeps the record, and
        keeps the handle too, so the next stop can try again.
        """
        process = self._process or self._unstopped
        record = self._record_text
        self._process = None
        self._unstopped = None
        if process is None:
            # Nothing of ours is recorded, so there is nothing of ours to
            # remove. Any record present belongs to another Wheelhouse, and
            # deleting it would make its running server unfindable.
            return

        if self._end_process(process):
            if record is not None:
                self._remove_own_record()
            return

        self._unstopped = process
        if record is None:
            # A server that would not stop and that nothing names is the
            # orphan the record exists to prevent, so this is the last chance
            # to write one. It happens when publication failed after the spawn
            # and start() called this to undo it.
            record = self._record_text if self._write_pid_file(process) else None
        if record is None:
            logger.error(
                "The local AI server at process %d is still running and could "
                "not be recorded. Ending Wheelhouse should still end it; if it "
                "does not, it must be stopped by hand.", process.pid,
            )
        else:
            logger.error(
                "The local AI server at process %d is still running. Its "
                "record is kept at %s so a later start can stop it.",
                process.pid, self._pid_file,
            )
