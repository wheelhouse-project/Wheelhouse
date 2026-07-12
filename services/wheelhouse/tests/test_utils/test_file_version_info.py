"""Tests for file_version_info.py - friendly app name lookup (wh-b0sch).

The resolver reads the FileDescription string from an EXE's VS_VERSIONINFO
resource via Win32 GetFileVersionInfo and returns it for the rejection-toast
title and body. Lookup is cached by process_id with a TTL of 300 seconds
(5 minutes). Failures fall back to the executable basename without the .exe
suffix (e.g. ``zed`` for ``zed.exe``).

Tests use an injectable lookup callable so they do not call real Win32 APIs.
"""

from __future__ import annotations

from typing import Optional

import pytest

from utils.file_version_info import FriendlyAppNameResolver


class _Clock:
    """Deterministic monotonic clock for TTL tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestFallback:
    def test_lookup_returns_none_falls_back_to_basename_without_extension(self):
        resolver = FriendlyAppNameResolver(
            lookup_callable=lambda pid: None,
        )
        assert resolver.resolve(1234, "zed.exe") == "zed"

    def test_fallback_handles_uppercase_extension(self):
        resolver = FriendlyAppNameResolver(
            lookup_callable=lambda pid: None,
        )
        assert resolver.resolve(1234, "Zed.EXE") == "Zed"

    def test_fallback_handles_no_extension(self):
        resolver = FriendlyAppNameResolver(
            lookup_callable=lambda pid: None,
        )
        assert resolver.resolve(1234, "weird") == "weird"

    def test_empty_fallback_returns_unknown(self):
        resolver = FriendlyAppNameResolver(
            lookup_callable=lambda pid: None,
        )
        assert resolver.resolve(1234, "") == "unknown"

    def test_zero_pid_skips_lookup_and_falls_back(self):
        called = []

        def _lookup(pid: int) -> Optional[str]:
            called.append(pid)
            return "Should Not Be Used"

        resolver = FriendlyAppNameResolver(lookup_callable=_lookup)
        assert resolver.resolve(0, "zed.exe") == "zed"
        assert called == []

    def test_lookup_raising_exception_falls_back(self):
        def _lookup(pid: int) -> Optional[str]:
            raise OSError("access denied")

        resolver = FriendlyAppNameResolver(lookup_callable=_lookup)
        assert resolver.resolve(1234, "zed.exe") == "zed"


class TestSuccessPath:
    def test_lookup_returns_friendly_name(self):
        resolver = FriendlyAppNameResolver(
            lookup_callable=lambda pid: "Zed Editor",
        )
        assert resolver.resolve(1234, "zed.exe") == "Zed Editor"

    def test_empty_string_from_lookup_falls_back(self):
        resolver = FriendlyAppNameResolver(
            lookup_callable=lambda pid: "",
        )
        assert resolver.resolve(1234, "zed.exe") == "zed"

    def test_whitespace_only_friendly_name_falls_back(self):
        resolver = FriendlyAppNameResolver(
            lookup_callable=lambda pid: "   ",
        )
        assert resolver.resolve(1234, "zed.exe") == "zed"


class TestCache:
    def test_repeated_resolve_calls_lookup_once_within_ttl(self):
        calls = []

        def _lookup(pid: int) -> Optional[str]:
            calls.append(pid)
            return "Zed Editor"

        clock = _Clock()
        resolver = FriendlyAppNameResolver(
            lookup_callable=_lookup, time_source=clock,
        )
        assert resolver.resolve(1234, "zed.exe") == "Zed Editor"
        assert resolver.resolve(1234, "zed.exe") == "Zed Editor"
        assert resolver.resolve(1234, "zed.exe") == "Zed Editor"
        assert calls == [1234]

    def test_ttl_expiry_triggers_refresh(self):
        calls = []

        def _lookup(pid: int) -> Optional[str]:
            calls.append(pid)
            return f"Name-{len(calls)}"

        clock = _Clock()
        resolver = FriendlyAppNameResolver(
            lookup_callable=_lookup, ttl_seconds=300.0, time_source=clock,
        )
        assert resolver.resolve(1234, "zed.exe") == "Name-1"
        clock.advance(299.0)
        assert resolver.resolve(1234, "zed.exe") == "Name-1"
        clock.advance(2.0)
        assert resolver.resolve(1234, "zed.exe") == "Name-2"
        assert calls == [1234, 1234]

    def test_different_pids_cached_separately(self):
        def _lookup(pid: int) -> Optional[str]:
            return {1: "App One", 2: "App Two"}.get(pid)

        resolver = FriendlyAppNameResolver(lookup_callable=_lookup)
        assert resolver.resolve(1, "one.exe") == "App One"
        assert resolver.resolve(2, "two.exe") == "App Two"
        assert resolver.resolve(1, "one.exe") == "App One"

    def test_failed_lookup_is_also_cached(self):
        # A pid whose EXE has no FileDescription (lookup returns None)
        # should fall back to basename, but the resolver should not
        # repeat the underlying Win32 calls on every rejection.
        calls = []

        def _lookup(pid: int) -> Optional[str]:
            calls.append(pid)
            return None

        resolver = FriendlyAppNameResolver(lookup_callable=_lookup)
        assert resolver.resolve(1234, "zed.exe") == "zed"
        assert resolver.resolve(1234, "zed.exe") == "zed"
        assert resolver.resolve(1234, "zed.exe") == "zed"
        assert calls == [1234]


class TestThreadSafety:
    def test_concurrent_resolves_do_not_raise(self):
        # The cache is not thread-safe; the resolver itself just guards
        # the dict update with a lock so concurrent resolves do not
        # corrupt internal state. Smoke-level assertion: many threads
        # hitting resolve on the same pid never raise.
        import threading

        def _lookup(pid: int) -> Optional[str]:
            return f"App-{pid}"

        resolver = FriendlyAppNameResolver(lookup_callable=_lookup)
        errors: list[BaseException] = []

        def worker():
            try:
                for _ in range(100):
                    resolver.resolve(99, "x.exe")
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


class TestPidReuse:
    """Regression test for wh-9weum.4.1 cache poisoning by PID reuse.

    Windows aggressively reuses PIDs. If a short-lived process exits
    and a different application is assigned the same PID inside the
    5-minute TTL, the cache must NOT return the dead application's
    friendly name. The cache key includes process_name to discriminate.
    """

    def test_recycled_pid_with_different_process_name_misses_cache(self):
        calls: list[tuple[int, ...]] = []

        def _lookup(pid: int):
            calls.append((pid,))
            # Indicates the resolver should try the EXE behind the PID.
            return f"App-{len(calls)}"

        clock = _Clock()
        resolver = FriendlyAppNameResolver(
            lookup_callable=_lookup, time_source=clock,
        )
        first = resolver.resolve(1234, "shortlived.exe")
        # PID gets recycled by the OS. Same pid, different process_name.
        second = resolver.resolve(1234, "different.exe")
        assert first == "App-1"
        assert second == "App-2"
        assert len(calls) == 2  # Both lookups ran.
