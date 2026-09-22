"""Make every Wheelhouse process find a working Microsoft Visual C++ runtime.

THE DEFECT THIS FIXES, measured on MavenCore (Windows 10 Pro 22H2 build
19045.6466) on 2026-09-19. The uv Python distribution ships vcruntime140.dll
and vcruntime140_1.dll 14.44.35211.0 beside python.exe, and ships no
msvcp140.dll, because CPython itself needs no C++ library. Every extension
module that does need one therefore falls through to
C:\\WINDOWS\\SYSTEM32\\msvcp140.dll, which on that machine is 14.29.30133.0 --
the Visual Studio 2019 level, a full toolset generation older than the
extensions. pysilero_vad's model load exits -1073741819 (0xC0000005) and
onnxruntime's import fails with "DLL load failed ... initialization routine
failed". The Windows event log named msvcp140.dll 14.29.30133.0 at fault
offset 0x13020 in all six crashes.

WHY A DIRECTORY AND NOT A COPY BESIDE EACH MODULE. A copy inside a package
folder is deleted whenever uv reinstalls that package, silently. This folder
belongs to Wheelhouse, sits beside the application folder rather than inside
it, and survives the re-install that replaces the application
(install-wheelhouse.ps1 deletes %LOCALAPPDATA%\\Wheelhouse\\app and unpacks it
again).

WHY THE CALL MUST COME FIRST. os.add_dll_directory cannot displace a library
the process has already loaded. Measured on MavenCore: a process that imports
PySide6 first never faults, because PySide6 ships its own 14.44 copy and
pysilero_vad then uses that one; a process that reaches pysilero_vad with
nothing loaded takes System32's copy and faults. So the call belongs above
each entry point's import block, and tests/test_runtime_dll_directory.py in
the wheelhouse service checks every entry point statically.

The shape of the helper follows the one already in
services/stt_providers/sherpa_offline_parakeet_stt_server/sherpa_engine.py,
which adds onnxruntime's capi directory for the same class of reason.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import wintypes
from pathlib import Path


log = logging.getLogger(__name__)

RUNTIME_DLL_NAME = "msvcp140.dll"

# THE ONE CONSTANT. A sibling of %LOCALAPPDATA%\Wheelhouse\app, never inside
# it: install-wheelhouse.ps1 deletes the application folder on every
# re-install and unpacks it again, and this folder has to outlive that.
RUNTIME_DLL_DIRECTORY = Path(
    os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
) / "Wheelhouse" / "runtime"

SYSTEM_RUNTIME_DLL = Path(
    os.environ.get("SystemRoot") or "C:\\Windows"
) / "System32" / RUNTIME_DLL_NAME

# The lowest system runtime version this project does not warn about.
# crewcut: the true minimum is not measured. 14.29.30133.0 faults and
# 14.44.35211.0 works; nothing between was tested, because testing it means
# changing the version on the one machine that reproduces the defect. 14.30 is
# the Visual Studio 2022 baseline, and the extension modules are built with a
# 2022 toolset, so a machine at or above it is not the case we measured.
# Measure the real minimum on a machine nobody needs as a reproducer, then
# raise or lower this pair.
MINIMUM_SYSTEM_RUNTIME = (14, 30)


def add_runtime_dll_directory():
    """Put the owned runtime folder on this process's library search path.

    Returns the handle os.add_dll_directory gave, or None when there was
    nothing worth adding. Never raises: this runs above the imports of every
    entry point, so an exception here would stop a process that would
    otherwise have started -- a worse outcome than the fault it prevents, and
    on every machine rather than the few that need it.
    """
    try:
        if sys.platform != "win32":
            return None
        folder = RUNTIME_DLL_DIRECTORY
        if not folder.is_dir():
            return None
        # The file being there is not enough. A copy older than the threshold
        # is the same library the fix exists to avoid, and a copy whose
        # version cannot be read counts as not trusted either -- the
        # criterion is "at or above the threshold", and an unreadable version
        # cannot be shown to meet it. See _owned_copy_is_trusted.
        if not _owned_copy_is_trusted():
            return None
        return os.add_dll_directory(str(folder))
    except Exception:
        return None


def _file_version(path: Path):
    """The four-part file version of a Windows binary, or None.

    Every ctypes prototype is declared in full. Without argtypes, ctypes
    narrows a 64-bit pointer to a 32-bit C int, and the call then reads
    nothing (measured while probing MavenCore on 2026-09-19).
    """
    try:
        if sys.platform != "win32" or not path.is_file():
            return None
        version_dll = ctypes.WinDLL("version", use_last_error=True)
        version_dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version_dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        version_dll.GetFileVersionInfoW.restype = wintypes.BOOL
        version_dll.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                     wintypes.DWORD, ctypes.c_void_p]
        version_dll.VerQueryValueW.restype = wintypes.BOOL
        version_dll.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                               ctypes.POINTER(ctypes.c_void_p),
                                               ctypes.POINTER(wintypes.UINT)]

        text = str(path)
        size = version_dll.GetFileVersionInfoSizeW(text, None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(text, 0, size, buffer):
            return None
        block = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version_dll.VerQueryValueW(buffer, "\\", ctypes.byref(block),
                                          ctypes.byref(length)):
            return None
        if not length.value:
            return None

        class FixedFileInfo(ctypes.Structure):
            _fields_ = [("dwSignature", wintypes.DWORD),
                        ("dwStrucVersion", wintypes.DWORD),
                        ("dwFileVersionMS", wintypes.DWORD),
                        ("dwFileVersionLS", wintypes.DWORD),
                        ("dwProductVersionMS", wintypes.DWORD),
                        ("dwProductVersionLS", wintypes.DWORD),
                        ("dwFileFlagsMask", wintypes.DWORD),
                        ("dwFileFlags", wintypes.DWORD),
                        ("dwFileOS", wintypes.DWORD),
                        ("dwFileType", wintypes.DWORD),
                        ("dwFileSubtype", wintypes.DWORD),
                        ("dwFileDateMS", wintypes.DWORD),
                        ("dwFileDateLS", wintypes.DWORD)]

        info = ctypes.cast(block, ctypes.POINTER(FixedFileInfo)).contents
        if info.dwSignature != 0xFEEF04BD:
            return None
        return (info.dwFileVersionMS >> 16, info.dwFileVersionMS & 0xFFFF,
                info.dwFileVersionLS >> 16, info.dwFileVersionLS & 0xFFFF)
    except Exception:
        return None


def system_runtime_version():
    """The version of the system msvcp140.dll, or None when unreadable."""
    return _file_version(SYSTEM_RUNTIME_DLL)


def _owned_copy_is_trusted():
    """True when the owned copy is there AND at or above the threshold.

    A copy whose version cannot be read is NOT trusted. The criterion is "at
    or above MINIMUM_SYSTEM_RUNTIME", and a version nobody can read cannot be
    shown to meet it. This is on purpose the opposite of the system copy,
    where an unreadable version stays silent: that file belongs to Windows
    and nothing is known about it, while this one was written by Wheelhouse's
    own installer, so a version this process cannot read means the install
    did not put there what it was meant to put there. The cost of being
    wrong is a notice the user did not need; the cost of the other choice is
    a process that faults with no notice at all.
    """
    version = _file_version(RUNTIME_DLL_DIRECTORY / RUNTIME_DLL_NAME)
    return version is not None and version[:2] >= MINIMUM_SYSTEM_RUNTIME


# THE USER TEXT, and why it carries no version number. Boss direction
# 2026-09-19, for David's approval: a version number is jargon the reader
# cannot act on, and 14.30 is a toolset baseline rather than a measured
# threshold, so printing it would claim more than the measurement supports.
# The numbers go to the log line beside the notice instead, which is the
# pattern AUDIO_DEVICE_MISSING_MESSAGE already set in
# services/stt_providers/shared/shared_audio/capture/winrt_capture.py.
#
# THE LENGTH IS LOAD-BEARING. utils/notice_text.py fits the message into
# plyer's szInfo field, WCHAR * 256, and shortens anything longer, so an
# over-long notice loses its tail. This text is measured against that limit
# by tests/test_runtime_dll_directory.py in the wheelhouse service. The path
# that shows it adds no prefix: stt/provider_env_check.py line 140 passes the
# message through unchanged under the title "Wheelhouse: Speech setup".
#
# David approved these words on QUESTIONS-2026-09-19.md item 20, which
# replaces items 13, 14 and 15. The last sentence is for someone who did
# select Yes and let Microsoft's installer finish: that installer sometimes
# needs a restart before Windows uses the new runtime, and without the
# sentence the notice tells that person to do the one thing they already
# did. The whole text has to stay within
# utils/notice_text.py's MAX_MESSAGE_WIDE_CHARACTERS, one below szInfo's
# WCHAR * 256 so the text plus a terminating NUL fits.
RUNTIME_NOTICE = (
    "Speech needs a newer Microsoft Visual C++ runtime. Run the Wheelhouse "
    "installer again and select Yes when Windows asks for permission. "
    "If you already did that, restart the computer."
)


def runtime_version_notice():
    """One sentence pair for the user, or None when nothing needs saying.

    Says something only when the owned folder holds no copy this process can
    trust AND the system copy cannot serve: either it is not there at all, or
    it is older than the extension modules need. With a trusted owned copy
    the process is already repaired, so a notice would only worry someone
    whose speech engine works.

    An owned copy at or above MINIMUM_SYSTEM_RUNTIME is what counts as
    trusted; one that is older, or whose version cannot be read, is not, and
    the system copy is then judged on its own. A system copy that is present
    but carries no readable version resource stays silent, because nothing is
    known about it.

    Logs the detail at ERROR beside the notice, because the user text
    carries no version number: the found and required versions when a
    version was read, and both paths when the file is missing.
    """
    if sys.platform != "win32":
        return None
    # The owned copy silences this only when it is at or above the threshold.
    # A copy whose version cannot be read counts as not trusted and silences
    # nothing: the criterion is "at or above the threshold", and an
    # unreadable version cannot be shown to meet it. See
    # _owned_copy_is_trusted for why this differs from the system copy below.
    if _owned_copy_is_trusted():
        return None
    if not SYSTEM_RUNTIME_DLL.is_file():
        # No system copy at all. The Microsoft Visual C++ Redistributable is
        # not part of Windows, so a computer on which no program ever
        # installed it has no msvcp140.dll anywhere, and every native
        # extension module that needs the C++ library fails to load. That
        # computer is worse off than MavenCore, whose copy is merely old,
        # and the remedy is the same one. Reading the version of a file that
        # is not there gives None, so this case has to be answered before
        # the version is read.
        log.error(
            "%s does not exist and %s holds no copy either, so every native "
            "extension module that needs the C++ library fails to load. "
            "Telling the user to install the Microsoft Visual C++ "
            "Redistributable (x64).",
            SYSTEM_RUNTIME_DLL, RUNTIME_DLL_DIRECTORY,
        )
        return RUNTIME_NOTICE
    version = system_runtime_version()
    if version is None:
        # The file is there but carries no readable version resource.
        # Nothing is known, so nothing is said.
        return None
    if version[:2] >= MINIMUM_SYSTEM_RUNTIME:
        return None
    found = ".".join(str(part) for part in version)
    needed = ".".join(str(part) for part in MINIMUM_SYSTEM_RUNTIME)
    log.error(
        "%s is version %s. The speech engine's extension modules are built "
        "against %s or newer, and %s holds no copy for this process to use "
        "instead, so a native module can fault inside the system library. "
        "Telling the user to install the Microsoft Visual C++ "
        "Redistributable (x64).",
        SYSTEM_RUNTIME_DLL, found, needed, RUNTIME_DLL_DIRECTORY,
    )
    return RUNTIME_NOTICE
