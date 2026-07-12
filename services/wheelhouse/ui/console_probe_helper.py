"""Console-probe helper subprocess: the ONLY component that AttachConsoles.

This module is the entry point for an isolated helper process. It exists so
the Logic process never binds to a foreign terminal's console. The Logic
process used to call ``FreeConsole()`` + ``AttachConsole(shell_pid)`` inline in
``PromptDetector._has_interactive_child`` to read a shell's console input mode.
That serialised on a module-level lock WITHIN the process, but it could not
block the logging thread (a ``StreamHandler`` on stderr) from writing during
the brief window the process was attached to the foreign console -- so any log
flush landed in the user's focused terminal's input box.

The fix moves all ``AttachConsole``/``FreeConsole``/``GetConsoleMode`` calls
into this helper process. The helper owns its own (foreign) console attachment
in complete isolation; the Logic process talks to it over a request/reply pipe
(``ui/console_probe_client.py``) and never touches a foreign console itself.

Wire protocol (newline-delimited JSON, one object per line):
  * request  (Logic -> helper):  {"pid": <int>}
  * response (helper -> Logic):   {"pid": <int>, "result": <bool>}

The helper handles ALL psutil/COM/Win32 errors internally and answers
``result: false`` on any failure (conservative: when unsure, report "not at a
prompt" so dictation does not redirect into a busy or unknown shell).

stderr discipline: the helper NEVER writes to stderr. The launcher additionally
redirects the helper's stderr to the OS null device, so even an unexpected
library write cannot leak text to a foreign console. All diagnostics, if any,
are intentionally suppressed -- a leak here is exactly the bug being fixed.
"""

from __future__ import annotations

import ctypes
import json
import sys
import threading
from ctypes import wintypes
from typing import TextIO

import psutil

# Shell / intermediary process names. These moved here verbatim from
# ``PromptDetector``; the detector no longer needs them in-process.
SHELL_NAMES = frozenset({
    "cmd.exe", "powershell.exe", "pwsh.exe",
    "bash.exe", "zsh.exe", "fish.exe",
})

INTERMEDIARY_NAMES = frozenset({
    "conpty.exe", "conhost.exe", "openconsole.exe",
})


# --- Windows Console API (only loaded inside the helper process) ---
# Guarded so the module can be imported on non-Windows CI for the pure
# request/reply tests; ``probe_at_prompt`` patches ``_kernel32`` in tests.
try:  # pragma: no cover - platform guard
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    _kernel32.AttachConsole.argtypes = [wintypes.DWORD]
    _kernel32.AttachConsole.restype = wintypes.BOOL

    _kernel32.FreeConsole.argtypes = []
    _kernel32.FreeConsole.restype = wintypes.BOOL

    # The console input mode is read from the console's own CONIN$ buffer, NOT
    # from GetStdHandle(STD_INPUT_HANDLE): this helper's standard handles are
    # redirected to pipes for its JSON request/reply protocol, and AttachConsole
    # does not overwrite std handles that were redirected at process creation.
    # GetStdHandle would therefore return the stdin pipe, on which GetConsoleMode
    # fails with ERROR_INVALID_HANDLE -- which made every shell look busy and
    # stopped the terminal editor from ever opening. Opening CONIN$ always yields
    # the input buffer of the console AttachConsole just bound.
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    _kernel32.CreateFileW.restype = ctypes.c_void_p  # HANDLE is pointer-sized

    _kernel32.GetConsoleMode.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)
    ]
    _kernel32.GetConsoleMode.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = wintypes.BOOL
except (OSError, AttributeError):  # pragma: no cover - non-Windows import path
    _kernel32 = None

# os is plain stdlib and always importable, so it is imported at module scope
# like the others; it is used for fd duplication in _has_interactive_child,
# which only runs on Windows.
import os

_ENABLE_LINE_INPUT = 0x0002
_ATTACH_PARENT_PROCESS = 0xFFFFFFFF

# CONIN$ open parameters for CreateFileW. Read access ONLY: GetConsoleMode needs
# no more, and this module must never hold write access to a foreign console
# (the leak this whole helper exists to prevent). FILE_SHARE_READ|FILE_SHARE_WRITE
# + OPEN_EXISTING is the documented way to obtain a usable handle to the active
# console's input buffer.
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# Serialises console attach/detach WITHIN the helper process. Unlike the old
# in-process lock, here it only competes with other helper requests -- there is
# no foreign logging thread in this process to leak during the attach window.
_console_lock = threading.Lock()


class ResponseChannelCorrupted(Exception):
    """Raised when the helper can no longer trust fd 1 (the response channel).

    The std-fd restore in ``_has_interactive_child`` re-binds fds 0/1/2 to
    their original pipes after the ``AttachConsole(shell_pid)`` window. fd 1 is
    the helper's stdout -- the channel its JSON response travels back to the
    ``ConsoleProbeClient`` on. If the restore of fd 1 fails, fd 1 may still be
    bound to the foreign shell's console (CONOUT$): every subsequent
    ``sys.stdout.write`` would leak the response ``{"pid": N, "result": ...}``
    into the user's focused terminal -- the exact "leak text into the foreign
    console" failure this whole split exists to eliminate (wh-jvrs.2.3).

    Rather than continue writing to a possibly-foreign fd 1, the helper treats
    itself as corrupted and exits ``run_loop`` so the client's EOF path spawns
    a clean replacement helper with an intact response channel. fds 0 and 2
    are NOT escalated: fd 0 (request channel) failing is caught downstream by
    the client's read timeout, and fd 2 is the discarded stderr.
    """


def _find_shells(proc: "psutil.Process") -> list["psutil.Process"]:
    shells: list[psutil.Process] = []
    try:
        for child in proc.children():
            child_name = child.name().lower()
            if child_name in SHELL_NAMES:
                shells.append(child)
            elif child_name in INTERMEDIARY_NAMES:
                shells.extend(_find_shells(child))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return shells


def _has_interactive_child(shell_pid: int) -> bool:
    """Detect an interactive TUI by reading the shell's console input mode.

    Interactive programs (vim, claude, python REPL) switch the console to raw
    mode by disabling ``ENABLE_LINE_INPUT``. Non-interactive programs leave the
    console in cooked mode (``ENABLE_LINE_INPUT`` on).

    Uses ``AttachConsole`` to temporarily attach to the shell's console, reads
    the input mode, then detaches. Saves and restores the standard file
    descriptors so ``FreeConsole`` does not corrupt them. Because this runs in
    the isolated helper process, a console attach here can never leak the Logic
    process's logging output into the foreign terminal.

    Returns True if raw mode (interactive TUI) is detected; False on cooked
    mode or any failure (conservative).

    Raises ``ResponseChannelCorrupted`` if the std-fd restore of fd 1 (the
    response channel) fails -- the caller must stop using stdout and let the
    helper recycle, rather than leak responses into the foreign console
    (wh-jvrs.2.3).
    """
    result = False
    fd1_restore_failed = False

    with _console_lock:
        # Duplicate std fds before detaching. FreeConsole can invalidate the
        # underlying handles; the dupes keep the original pipe objects alive so
        # we can restore after.
        saved_fds = {}
        for fd_num in (0, 1, 2):
            try:
                saved_fds[fd_num] = os.dup(fd_num)
            except OSError:
                pass

        try:
            _kernel32.FreeConsole()

            if not _kernel32.AttachConsole(shell_pid):
                # AttachConsole failed (e.g. ConPTY does not support it).
                result = False
            else:
                try:
                    # Read the input mode from CONIN$, never from GetStdHandle:
                    # under the helper's pipe-redirected stdio GetStdHandle
                    # returns the stdin pipe and GetConsoleMode fails on it.
                    # CONIN$ resolves to the console AttachConsole(shell_pid)
                    # just bound, so the mode read is correct regardless of how
                    # the helper's own std handles are wired.
                    conin = _kernel32.CreateFileW(
                        "CONIN$",
                        _GENERIC_READ,
                        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                        None,
                        _OPEN_EXISTING,
                        0,
                        None,
                    )
                    if not conin or conin == _INVALID_HANDLE_VALUE:
                        result = False
                    else:
                        try:
                            mode = wintypes.DWORD()
                            if not _kernel32.GetConsoleMode(
                                conin, ctypes.byref(mode)
                            ):
                                result = False
                            else:
                                result = not (mode.value & _ENABLE_LINE_INPUT)
                        finally:
                            _kernel32.CloseHandle(conin)
                finally:
                    _kernel32.FreeConsole()
        finally:
            # Restore std fds to their original pipes/handles.
            for fd_num, saved_fd in saved_fds.items():
                try:
                    os.dup2(saved_fd, fd_num)
                except OSError:
                    # fd 1 is the response channel back to the client. A failed
                    # restore there may leave fd 1 bound to the foreign console;
                    # flag it so run_loop recycles instead of writing the next
                    # response into the user's terminal (wh-jvrs.2.3). fds 0/2
                    # are not escalated (see ResponseChannelCorrupted docstring).
                    if fd_num == 1:
                        fd1_restore_failed = True
                finally:
                    try:
                        os.close(saved_fd)
                    except OSError:
                        pass

            # Best-effort re-attach to the parent's console session. Under the
            # normal launcher/GUI start path the parent (the Logic process) is a
            # multiprocessing-spawned child with no console of its own, so this
            # call normally fails and the helper runs CONSOLE-LESS between
            # probes. That is fine: the helper talks only over its stdin/stdout
            # pipes, never a console, and the next probe's FreeConsole() is a
            # no-op when console-less before AttachConsole(shell_pid) re-binds.
            # The BOOL return is intentionally ignored (wh-jvrs.1.1.5).
            _kernel32.AttachConsole(_ATTACH_PARENT_PROCESS)

    # Raised OUTSIDE the lock and the finally so the console attachment has
    # already been fully unwound. The response channel may now be foreign;
    # signal the caller to recycle rather than write to it (wh-jvrs.2.3).
    if fd1_restore_failed:
        raise ResponseChannelCorrupted(
            "std-fd restore of fd 1 (response channel) failed"
        )

    return result


def probe_at_prompt(terminal_pid: int) -> bool:
    """Return True iff the terminal at ``terminal_pid`` is at a shell prompt.

    This is the at-prompt logic that used to live in
    ``PromptDetector._check_prompt``. It walks the process subtree to find the
    interactive shell(s), and -- for a shell that has children -- consults the
    console input mode (raw vs cooked) to tell an interactive TUI apart from a
    blocking non-interactive command.

    Conservative on every error: returns False rather than raising, with ONE
    exception -- ``ResponseChannelCorrupted`` propagates from
    ``_has_interactive_child`` when the response channel (fd 1) can no longer
    be trusted, so ``run_loop`` can recycle instead of writing to a possibly
    foreign console (wh-jvrs.2.3).
    """
    try:
        proc = psutil.Process(terminal_pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    try:
        proc_name = proc.name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    # Legacy console: the shell IS the top-level process.
    if proc_name in SHELL_NAMES:
        try:
            return len(proc.children()) == 0
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    # Modern terminal (e.g. Windows Terminal): find shells in the subtree.
    shells = _find_shells(proc)
    if not shells:
        return False

    # Conservative across ALL shells in the subtree: in a multi-tab terminal
    # _find_shells returns every tab's shell. ANY shell that is busy (has a
    # non-interactive child in cooked mode) means the terminal is not safely
    # at a prompt, so dictation must not redirect into it. Returning True on
    # the FIRST shell that happens to have an interactive TUI child would mask
    # a later busy tab (e.g. tab1 vim + tab2 git clone), violating the
    # fail-closed posture. So: any shell with a non-interactive child -> False;
    # only return True after every shell has been verified idle or TUI-only.
    for shell in shells:
        try:
            children = shell.children()
            if children:
                # Shell has children -- interactive TUI (raw mode) or a
                # blocking non-interactive command (cooked mode)? A blocking
                # command anywhere means busy: fail closed immediately.
                if not _has_interactive_child(shell.pid):
                    return False
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    return True


def run_loop(stdin: TextIO, stdout: TextIO) -> None:
    """Read requests from ``stdin``, write responses to ``stdout``.

    Each input line is a JSON object ``{"pid": <int>}``. Each output line is a
    JSON object ``{"pid": <int>, "result": <bool>}``. The loop ends on EOF
    (empty readline). Malformed requests and probe failures both yield a
    ``result: false`` response so the client always gets exactly one reply per
    request and never blocks.

    If ``probe_at_prompt`` raises ``ResponseChannelCorrupted`` -- meaning the
    std-fd restore of fd 1 failed and ``stdout`` may now point at a foreign
    console -- the loop returns WITHOUT writing a response (writing it would
    leak into the user's terminal, the exact bug this split prevents). The
    client's read then times out, its EOF/recycle path fires, and a clean
    helper with an intact response channel is spawned (wh-jvrs.2.3).
    """
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        pid = None
        result = False
        try:
            payload = json.loads(line)
            pid = int(payload["pid"])
            result = bool(probe_at_prompt(pid))
        except ResponseChannelCorrupted:
            # fd 1 may be foreign; do NOT write the response. Exit so the
            # client recycles us into a clean helper (wh-jvrs.2.3).
            return
        except Exception:
            # Malformed request or probe failure: answer conservatively.
            result = False
        response = {"pid": pid, "result": result}
        try:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
        except Exception:
            # If we cannot answer, the client's read times out and fails
            # closed; nothing more we can safely do here.
            return


def main() -> None:  # pragma: no cover - process entry point
    """Process entry point. Drives ``run_loop`` over the real std streams.

    Never writes to stderr. Uses ``sys.stdin`` / ``sys.stdout`` in text mode
    with newline-delimited JSON.
    """
    run_loop(sys.stdin, sys.stdout)


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
