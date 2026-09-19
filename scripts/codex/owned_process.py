"""Bound Windows process trees for isolated test and mutation runners.

run_owned returns CompletedProcess or raises TimeoutExpired only after owned
process cleanup is confirmed. Both expose cleanup_confirmed=True, as do other
ordinary exceptions propagated after confirmed cleanup (including interrupts).
CleanupUnconfirmedError carries cleanup_confirmed=False and takes precedence
over timeout, interruption, or launch errors when exit cannot be proved.

Callers own source backups. Catch CleanupUnconfirmedError before broad OSError,
refuse source restoration and another mutation, and preserve the error/evidence.
This module never reads or restores source. It captures only private-Job members,
uses no process-name or parent-PID scan, and does not change dependency state.
Execution requires Windows Job Objects; unsupported platforms fail before launch.
"""
from __future__ import annotations

import ctypes
from itertools import islice
import math
from pathlib import Path
import subprocess
import sys
import time
import uuid

CLEANUP_SECONDS = 2
OUTPUT_LIMIT = 8 * 1024 * 1024
_DIAGNOSTIC_MEMBER_LIMIT = 64


class CleanupUnconfirmedError(OSError):
    """No caller may restore mutated source or continue after this failure."""

    cleanup_confirmed = False

    def __init__(self, message, stdout_path=None, stderr_path=None, *, cleanup_diagnostics=None):
        if cleanup_diagnostics is not None:
            message += f"; cleanup diagnostics: {cleanup_diagnostics!r}"
        super().__init__(message)
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.cleanup_diagnostics = cleanup_diagnostics

if sys.platform == "win32":
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t), ("process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", _BasicLimits), ("io", ctypes.c_ulonglong * 6),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
        ]

    class _Accounting(ctypes.Structure):
        _fields_ = [
            ("times", ctypes.c_longlong * 4), ("page_faults", wintypes.DWORD),
            ("total", wintypes.DWORD), ("active", wintypes.DWORD),
            ("terminated", wintypes.DWORD),
        ]

class _OwnedJob:
    """Private Job ownership; the gated bootstrap cannot launch before assign."""

    def __init__(self) -> None:
        self.process_handles = {}
        self.dll = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": (wintypes.HANDLE, [wintypes.LPVOID, wintypes.LPCWSTR]),
            "SetInformationJobObject": (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]),
            "QueryInformationJobObject": (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID]),
            "AssignProcessToJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
            "TerminateJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
            "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        }
        for name, (restype, argtypes) in signatures.items():
            function = getattr(self.dll, name)
            function.restype, function.argtypes = restype, argtypes
        self.handle = self.dll.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.dll.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        # Popen owns this live handle: no PID reuse between lookup and assign.
        if not self.dll.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def active(self) -> int:
        info = _Accounting()
        if not self.dll.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def close(self) -> None:
        try:
            if self.handle:
                if not self.dll.CloseHandle(self.handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handle = None
        finally:
            # An earlier enumeration error may bypass the normal wait cleanup.
            for handle in self.process_handles.values():
                self.dll.CloseHandle(handle)
            self.process_handles.clear()

    def stop(self, deadline: float) -> None:
        if not self.dll.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        while self.active():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError("Owned descendants did not stop within cleanup grace period.")
            time.sleep(min(0.02, remaining))

_BOOTSTRAP = """
import subprocess, sys
if sys.stdin.buffer.read(1) != b'G':
    sys.exit(125)
try:
    result = subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL)
except OSError as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(127)
sys.exit(result)
"""

def _capture_output(stream):
    # A fixed snapshot of a regular file never waits for pipe EOF. Refuse to
    # score oversized output, since truncation could hide a catcher failure.
    stream.seek(0)
    data = stream.read(OUTPUT_LIMIT + 1)
    if len(data) > OUTPUT_LIMIT:
        raise OSError("Process output exceeds 8 MiB; full raw log retained, no verdict")
    return data.decode("utf-8", errors="replace")

def _job_total(job):
    info = _Accounting()
    if not job.dll.QueryInformationJobObject(
        job.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return info.total

def _owned_process_handles(job, deadline):
    from ctypes import wintypes

    dll = job.dll
    dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    dll.IsProcessInJob.restype = wintypes.BOOL
    dll.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    dll.WaitForSingleObject.restype = wintypes.DWORD
    capacity = 64
    while True:
        if time.monotonic() >= deadline or capacity > 4096:
            raise OSError("Cannot enumerate owned processes within cleanup bound")

        class ProcessIds(ctypes.Structure):
            _fields_ = [("assigned", wintypes.DWORD), ("count", wintypes.DWORD),
                        ("ids", ctypes.c_size_t * capacity)]

        processes = ProcessIds()
        if dll.QueryInformationJobObject(
            job.handle, 3, ctypes.byref(processes), ctypes.sizeof(processes), None,
        ):
            if processes.count == processes.assigned:
                break
        elif ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
            raise ctypes.WinError(ctypes.get_last_error())
        capacity = max(capacity * 2, processes.assigned)
    # Keep membership-verified handles from every observation until cleanup.
    # A terminating process can leave the active PID list before its handle
    # signals. Keeping that handle also prevents reuse of its process identity.
    handles = job.process_handles
    for pid in processes.ids[:processes.count]:
        if pid in handles:
            continue
        if time.monotonic() >= deadline:
            raise OSError("Opening owned process handles exceeded cleanup bound")
        handle = dll.OpenProcess(0x100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not handle:
            if ctypes.get_last_error() == 87:  # Lifetime accounting still requires exit proof.
                continue
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            member = wintypes.BOOL()
            if not dll.IsProcessInJob(handle, job.handle, ctypes.byref(member)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not member.value:
                raise OSError("Job membership changed while opening process handles")
        except BaseException:
            dll.CloseHandle(handle)
            raise
        handles[pid] = handle
    return list(handles.values())

def _stop_job_and_wait(job, deadline):
    # TerminateJobObject, like TerminateProcess, is asynchronous. Accounting can
    # reach zero while a child's kernel handle is still unsignaled. Keep live,
    # membership-verified handles before termination; never kill by PID lookup.
    total = _job_total(job)
    handles = _owned_process_handles(job, deadline)
    observed_pids = getattr(job, "process_handles", {})
    # Snapshot only values already observed. The bounded lists share handle
    # iteration order; omitted entries still undergo every original exit check.
    diagnostics = {
        "initial_lifetime_total": total, "final_lifetime_total": None,
        "observed_handle_count": len(handles),
        "observed_process_ids": list(islice(observed_pids, _DIAGNOSTIC_MEMBER_LIMIT)),
        "omitted_process_ids": max(0, len(observed_pids) - _DIAGNOSTIC_MEMBER_LIMIT),
        "wait_results": [], "omitted_wait_results": 0,
    }
    try:
        job.stop(deadline)
        for handle in handles:
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            result = job.dll.WaitForSingleObject(handle, remaining_ms)
            if len(diagnostics["wait_results"]) < _DIAGNOSTIC_MEMBER_LIMIT:
                diagnostics["wait_results"].append(result)
            else:
                diagnostics["omitted_wait_results"] += 1
            if result == 258:
                raise OSError("Descendant process handle did not signal within cleanup grace")
            if result != 0:
                raise ctypes.WinError(ctypes.get_last_error())
        # Check after the known parents have signaled too: a spawn already in
        # progress during termination can complete while their I/O drains.
        final_total = _job_total(job)
        diagnostics["final_lifetime_total"] = final_total
        if final_total != total:
            raise OSError("Job membership grew during cleanup; descendant exit unconfirmed")
        if final_total != len(handles):
            # A short-lived member can start and exit between observations.
            # Stable accounting alone cannot prove when its handle signaled.
            raise OSError("Lifetime Job member exit is unconfirmed: process handle was not observed")
    except BaseException as exc:
        # Caches are closed below; preserve the missing proof before they go.
        # These observations explain refusal, never establish cleanup success.
        exc.cleanup_diagnostics = diagnostics
        raise
    finally:
        for handle in handles:
            job.dll.CloseHandle(handle)
        if hasattr(job, "process_handles"):
            job.process_handles.clear()


def run_owned(command, cwd, env, timeout_seconds, evidence_dir):
    """Run argv in a private Windows Job, with one two-second cleanup grace.

    stdout/stderr are text snapshots with complete raw files retained. More than
    eight MiB in either stream is an error, never a truncated success. Returned
    results and propagated exceptions expose cleanup_confirmed; an uncertain
    process exit always raises CleanupUnconfirmedError. Output paths are exposed
    as stdout_path/stderr_path on the result or exception once allocated.
    Cleanup uncertainty retains available counts and bounded member/wait values
    in cleanup_diagnostics and its message; unknown observations remain None.
    """
    cleanup_confirmed = True
    stdout_path = stderr_path = None
    try:
        if sys.platform != "win32":
            raise OSError("Owned execution requires Windows Job Object ownership")
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("command must be a nonempty argv sequence")
        timeout_seconds = float(timeout_seconds)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        command = list(command)
        cwd, evidence_dir = Path(cwd).resolve(), Path(evidence_dir).resolve()
        env = dict(env)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        stdout_path = evidence_dir / f"{run_id}.stdout.log"
        stderr_path = evidence_dir / f"{run_id}.stderr.log"
        deadline = time.monotonic() + timeout_seconds
        job = process = None
        job_exited = launcher_exited = True
        timed_out = False
        pending = None
        problems = []
        with stdout_path.open("w+b") as stdout, stderr_path.open("w+b") as stderr:
            try:
                job = _OwnedJob()
                cleanup_confirmed = job_exited = launcher_exited = False
                process = subprocess.Popen(
                    # The base interpreter avoids a Windows venv redirector
                    # spawning outside the Job before assignment can occur.
                    [sys._base_executable, "-I", "-S", "-c", _BOOTSTRAP, *command],
                    cwd=cwd, env=env, stdin=subprocess.PIPE,
                    stdout=stdout, stderr=stderr,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                job.assign(process)
                process.stdin.write(b"G")
                process.stdin.close()
                while True:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    _owned_process_handles(job, deadline)
                    if process.poll() is not None and job.active() == 0:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    time.sleep(min(0.02, remaining))
            except BaseException as exc:
                pending = exc
            finally:
                cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
                if job is not None:
                    try:
                        _stop_job_and_wait(job, cleanup_deadline)
                        job_exited = True
                    except BaseException as exc:
                        problems.append(exc)
                    finally:
                        try:
                            job.close()
                        except BaseException as exc:
                            problems.append(exc)
                if process is not None:
                    try:
                        if process.poll() is None:
                            # Assignment refusal leaves only this gated launcher
                            # outside the Job. Popen retains its exact live handle.
                            process.kill()
                        if process.stdin and not process.stdin.closed:
                            process.stdin.close()
                        process.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
                        launcher_exited = True
                    except BaseException as exc:
                        problems.append(exc)
                cleanup_confirmed = job_exited and (process is None or launcher_exited)
            if not cleanup_confirmed:
                detail = "; ".join(str(exc) or type(exc).__name__ for exc in problems)
                raise CleanupUnconfirmedError(
                    "Owned process exit is unconfirmed: " + detail,
                    stdout_path, stderr_path,
                    cleanup_diagnostics={
                        "timed_out": timed_out,
                        "pending_exception_type": type(pending).__name__ if pending is not None else None,
                        "cleanup_failures": [
                            {"exception_type": type(exc).__name__,
                             "job": getattr(exc, "cleanup_diagnostics", None)}
                            for exc in problems
                        ],
                    },
                ) from (pending or (problems[0] if problems else None))
            if pending is not None:
                raise pending
            if problems:
                raise problems[0]
            captured = [_capture_output(stream) for stream in (stdout, stderr)]
        if timed_out:
            raise subprocess.TimeoutExpired(command, timeout_seconds, output=captured[0], stderr=captured[1])
        result = subprocess.CompletedProcess(command, process.returncode, *captured)
        result.cleanup_confirmed = True
        result.stdout_path, result.stderr_path = stdout_path, stderr_path
        return result
    except BaseException as exc:
        # Even a second interruption during cleanup cannot turn unknown exit
        # into an apparently safe timeout, KeyboardInterrupt, or OSError.
        if not cleanup_confirmed and not isinstance(exc, CleanupUnconfirmedError):
            raise CleanupUnconfirmedError(
                "Owned process exit is unconfirmed", stdout_path, stderr_path,
            ) from exc
        if cleanup_confirmed:
            exc.cleanup_confirmed = True
        exc.stdout_path, exc.stderr_path = stdout_path, stderr_path
        raise
