"""Report the Wheelhouse version for the About dialog.

There are two places the version can be read from and no single place that
works in both a source checkout and a build:

- In a source checkout the release process writes it to the ``VERSION`` file
  at the top of the repository. That file is three directories above this
  module and is not part of what a build bundles.
- A build has it stamped into the executable's own version resource by
  PyInstaller, which is where Windows reads it from for the file properties
  dialog.

So this asks whichever question fits the way the program is running, and
answers ``"unknown"`` rather than raising if neither can be answered. Nothing
depends on the version except the text of a dialog.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

UNKNOWN_VERSION = "unknown"


def get_app_version() -> str:
    """Return the running program's version, or ``"unknown"``."""
    if getattr(sys, "frozen", False):
        stamped = _version_from_executable()
        if stamped:
            return stamped
        return UNKNOWN_VERSION

    return _version_from_repository_file() or UNKNOWN_VERSION


def _version_from_repository_file() -> str:
    """Read the VERSION file at the top of the repository."""
    try:
        # utils -> wheelhouse -> services -> repository root.
        version_file = Path(__file__).resolve().parents[3] / "VERSION"
        return version_file.read_text(encoding="utf-8").strip()
    except Exception as exc:
        logger.debug("Could not read the VERSION file: %s", exc)
        return ""


def _version_from_executable() -> str:
    """Read FileVersion from this executable's own version resource."""
    try:
        import ctypes
        from ctypes import wintypes

        version_api = ctypes.WinDLL("version", use_last_error=True)

        GetFileVersionInfoSizeW = version_api.GetFileVersionInfoSizeW
        GetFileVersionInfoSizeW.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        GetFileVersionInfoSizeW.restype = wintypes.DWORD

        GetFileVersionInfoW = version_api.GetFileVersionInfoW
        GetFileVersionInfoW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        ]
        GetFileVersionInfoW.restype = wintypes.BOOL

        VerQueryValueW = version_api.VerQueryValueW
        VerQueryValueW.argtypes = [
            wintypes.LPCVOID, wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT),
        ]
        VerQueryValueW.restype = wintypes.BOOL

        exe_path = sys.executable
        handle_var = wintypes.DWORD(0)
        info_size = GetFileVersionInfoSizeW(exe_path, ctypes.byref(handle_var))
        if info_size == 0:
            return ""

        info_buf = ctypes.create_string_buffer(info_size)
        if not GetFileVersionInfoW(exe_path, 0, info_size, info_buf):
            return ""

        # The numeric block, which every stamped executable has, rather than
        # the string table, which needs the right language and code page.
        value_ptr = wintypes.LPVOID()
        value_len = wintypes.UINT(0)
        if not VerQueryValueW(
            info_buf, "\\", ctypes.byref(value_ptr), ctypes.byref(value_len),
        ):
            return ""
        if not value_ptr.value or value_len.value < 52:
            return ""

        class VSFixedFileInfo(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
            ]

        info = VSFixedFileInfo.from_address(int(value_ptr.value))
        return "{}.{}.{}.{}".format(
            info.dwFileVersionMS >> 16,
            info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16,
            info.dwFileVersionLS & 0xFFFF,
        )
    except Exception as exc:
        logger.debug("Could not read the executable's version: %s", exc)
        return ""
