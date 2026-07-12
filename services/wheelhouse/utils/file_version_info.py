"""Friendly app name lookup via Win32 GetFileVersionInfo (wh-b0sch).

The rejection-toast pipeline needs a human-readable name for the focused
application -- ``Zed`` rather than ``zed.exe``. This module reads the
``FileDescription`` string from the executable's VS_VERSIONINFO resource
using Win32 ``GetFileVersionInfoW`` and ``VerQueryValueW``. Lookups are
cached by process_id with a 5 minute TTL so a busy session that keeps
rejecting in the same target does not repeat the syscalls per word.

Failure path: if any step (``OpenProcess``, ``QueryFullProcessImageNameW``,
``GetFileVersionInfoSizeW``, ``GetFileVersionInfoW``, ``VerQueryValueW``)
fails or returns an empty string, the resolver falls back to the
executable basename without the ``.exe`` suffix supplied by the caller
(usually the captured UIContext's ``process_name``). The resolver never
raises; the rejection-toast pipeline must always have something to show.

Privacy: this module reads only the executable's static metadata. It
does not read process command line, window title, or any user content.

Reference: wh-b0sch (Phase 2 friendly-name lookup), wh-9weum Phase 2,
parent epic wh-fc1x.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


_DEFAULT_TTL_SECONDS = 300.0
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PATH = 32768  # Wide-path limit; QueryFullProcessImageNameW honors this.


class FriendlyAppNameResolver:
    """Cached friendly-name lookup keyed by process id.

    The resolver delegates the actual Win32 work to ``lookup_callable``
    so tests can inject a deterministic stand-in. Production code uses
    the module-level ``default_resolver`` (constructed with
    :func:`_lookup_via_win32`).
    """

    def __init__(
        self,
        *,
        lookup_callable: Callable[[int], Optional[str]],
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        time_source: Optional[Callable[[], float]] = None,
    ) -> None:
        self._lookup = lookup_callable
        self._ttl_seconds = ttl_seconds
        self._time_source: Callable[[], float] = (
            time_source or time.monotonic
        )
        # (process_id, process_name) -> (cached_friendly_name_or_None, stored_at).
        # wh-9weum.4.1: include process_name in the key so a Windows PID
        # recycled inside the TTL window does not return the stale name
        # for an unrelated process. The captured UIContext carries
        # process_name from a per-utterance enumeration; if the new
        # process under the recycled PID has a different exe name, the
        # cache misses and the lookup runs fresh.
        self._cache: dict[
            tuple[int, str], tuple[Optional[str], float],
        ] = {}
        self._lock = threading.Lock()

    def resolve(self, process_id: int, fallback_process_name: str) -> str:
        """Return the friendly app name for ``process_id``.

        ``fallback_process_name`` is the captured UIContext's
        ``process_name`` (e.g. ``"zed.exe"``). It is used to derive the
        fallback (basename without extension) when the Win32 lookup
        fails or returns an empty string.

        Caches the lookup outcome (including ``None``) so a process
        whose executable has no FileDescription does not repeat the
        syscalls per word.
        """

        if process_id <= 0:
            return _basename_fallback(fallback_process_name)

        # wh-9weum.4.1: cache key is (pid, process_name) so a recycled
        # PID with a different exe does not inherit the previous
        # process's friendly name. Lower-case the process name so a
        # caller that captured "Zed.exe" and another that captured
        # "zed.exe" share the same cache slot.
        key = (process_id, (fallback_process_name or "").lower())
        found, cached = self._lookup_cached(key)
        friendly = (
            cached
            if found
            else self._do_lookup_and_cache(process_id, key)
        )

        if friendly:
            return friendly
        return _basename_fallback(fallback_process_name)

    def _lookup_cached(
        self, key: tuple[int, str],
    ) -> tuple[bool, Optional[str]]:
        now = self._time_source()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return False, None
            value, stored_at = entry
            if now - stored_at >= self._ttl_seconds:
                del self._cache[key]
                return False, None
            return True, value

    def _do_lookup_and_cache(
        self, process_id: int, key: tuple[int, str],
    ) -> Optional[str]:
        try:
            raw = self._lookup(process_id)
        except Exception as exc:
            logger.debug(
                "file_version_info: lookup raised for pid=%d: %s",
                process_id, exc,
            )
            raw = None

        if raw is None:
            normalised: Optional[str] = None
        else:
            stripped = raw.strip()
            normalised = stripped if stripped else None

        now = self._time_source()
        with self._lock:
            self._cache[key] = (normalised, now)
        return normalised


def _basename_fallback(process_name: str) -> str:
    """Strip a trailing ``.exe`` (case-insensitive) from ``process_name``.

    Returns ``"unknown"`` when ``process_name`` is empty so the toast
    body always has something to show.
    """

    name = (process_name or "").strip()
    if not name:
        return "unknown"
    lowered = name.lower()
    if lowered.endswith(".exe"):
        return name[:-4]
    return name


def _lookup_via_win32(process_id: int) -> Optional[str]:
    """Read ``FileDescription`` from the executable behind ``process_id``.

    Returns the FileDescription string, or ``None`` on any failure
    (process gone, access denied, no version resource, missing
    StringFileInfo, etc.). Never raises.

    The caller is responsible for the basename fallback when this
    returns ``None``.
    """

    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        version = ctypes.WinDLL("version", use_last_error=True)
    except OSError:
        return None

    OpenProcess = kernel32.OpenProcess
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenProcess.restype = wintypes.HANDLE

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    QueryFullProcessImageNameW = kernel32.QueryFullProcessImageNameW
    QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    QueryFullProcessImageNameW.restype = wintypes.BOOL

    GetFileVersionInfoSizeW = version.GetFileVersionInfoSizeW
    GetFileVersionInfoSizeW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    GetFileVersionInfoSizeW.restype = wintypes.DWORD

    GetFileVersionInfoW = version.GetFileVersionInfoW
    GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    ]
    GetFileVersionInfoW.restype = wintypes.BOOL

    VerQueryValueW = version.VerQueryValueW
    VerQueryValueW.argtypes = [
        wintypes.LPCVOID, wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT),
    ]
    VerQueryValueW.restype = wintypes.BOOL

    handle = OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(process_id),
    )
    if not handle:
        return None

    try:
        buf = ctypes.create_unicode_buffer(_MAX_PATH)
        size = wintypes.DWORD(_MAX_PATH)
        ok = QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(size),
        )
        if not ok:
            return None
        exe_path = buf.value
        if not exe_path or not os.path.exists(exe_path):
            return None
    finally:
        CloseHandle(handle)

    handle_var = wintypes.DWORD(0)
    info_size = GetFileVersionInfoSizeW(exe_path, ctypes.byref(handle_var))
    if info_size == 0:
        return None

    info_buf = ctypes.create_string_buffer(info_size)
    if not GetFileVersionInfoW(exe_path, 0, info_size, info_buf):
        return None

    translation_ptr = wintypes.LPVOID()
    translation_len = wintypes.UINT(0)
    if not VerQueryValueW(
        info_buf, "\\VarFileInfo\\Translation",
        ctypes.byref(translation_ptr), ctypes.byref(translation_len),
    ):
        return None
    translation_addr = translation_ptr.value
    if translation_len.value < 4 or not translation_addr:
        return None

    # Translation table is an array of (lang_id, codepage) WORD pairs.
    # Try each pair in order until one yields a non-empty FileDescription.
    pair_count = translation_len.value // 4
    word_array = (wintypes.WORD * (pair_count * 2)).from_address(
        translation_addr
    )
    for i in range(pair_count):
        lang_id = word_array[i * 2]
        codepage = word_array[i * 2 + 1]
        sub_block = (
            f"\\StringFileInfo\\{lang_id:04x}{codepage:04x}\\FileDescription"
        )
        value_ptr = wintypes.LPVOID()
        value_len = wintypes.UINT(0)
        if not VerQueryValueW(
            info_buf, sub_block,
            ctypes.byref(value_ptr), ctypes.byref(value_len),
        ):
            continue
        value_addr = value_ptr.value
        if value_len.value == 0 or value_addr is None or value_addr == 0:
            continue
        description = ctypes.wstring_at(int(value_addr), value_len.value)
        # ``wstring_at`` includes the trailing NUL when value_len includes
        # it; strip and trim.
        description = description.rstrip("\x00").strip()
        if description:
            return description

    return None


# Module-level default. Constructed lazily by ``get_default_resolver`` so
# tests that import the module do not pay for the ctypes setup until the
# real path is exercised.
_default_resolver: Optional[FriendlyAppNameResolver] = None
_default_lock = threading.Lock()


def get_default_resolver() -> FriendlyAppNameResolver:
    """Return the process-wide default resolver, constructing it on demand."""

    global _default_resolver
    with _default_lock:
        if _default_resolver is None:
            _default_resolver = FriendlyAppNameResolver(
                lookup_callable=_lookup_via_win32,
            )
        return _default_resolver
