"""Tests for shared_audio.thread_priority -- Windows thread priority elevation.

The elevation exists so the audio capture and consumer threads keep getting
scheduled when the whole machine is saturated by bulk compute (the root cause
of the wh-stt-audio-consumer-behind-realtime overflow bursts). Tests run the
real Windows API round-trip in a scratch thread so the pytest main thread's
priority is never changed.
"""
import sys
import threading

import pytest

import shared_audio.thread_priority as thread_priority
from shared_audio.thread_priority import (
    elevate_current_thread,
    get_current_thread_priority,
)

_windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows thread priorities only"
)


def _run_in_thread(fn):
    """Run fn in a dedicated thread and return its result (or raise)."""
    result = {}

    def wrapper():
        try:
            result["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised in caller
            result["error"] = e

    t = threading.Thread(target=wrapper)
    t.start()
    t.join(timeout=5.0)
    if "error" in result:
        raise result["error"]
    return result["value"]


@_windows_only
class TestElevateCurrentThread:
    def test_invalid_level_raises(self):
        with pytest.raises(ValueError):
            elevate_current_thread("supersonic")

    def test_elevate_highest_round_trip(self):
        def body():
            ok = elevate_current_thread("highest")
            return ok, get_current_thread_priority()

        ok, priority = _run_in_thread(body)
        assert ok is True
        assert priority == 2  # THREAD_PRIORITY_HIGHEST

    def test_elevate_time_critical_round_trip(self):
        def body():
            ok = elevate_current_thread("time_critical")
            return ok, get_current_thread_priority()

        ok, priority = _run_in_thread(body)
        assert ok is True
        assert priority == 15  # THREAD_PRIORITY_TIME_CRITICAL

    def test_default_thread_priority_is_normal(self):
        priority = _run_in_thread(get_current_thread_priority)
        assert priority == 0  # THREAD_PRIORITY_NORMAL


class _FakeKernel32:
    """Stand-in for the kernel32 handle so failure branches run anywhere."""

    def __init__(self, set_result=1, set_raises=None, get_result=0):
        self._set_result = set_result
        self._set_raises = set_raises
        self._get_result = get_result

    def GetCurrentThread(self):
        return 0x1234

    def SetThreadPriority(self, handle, level):
        if self._set_raises is not None:
            raise self._set_raises
        return self._set_result

    def GetThreadPriority(self, handle):
        return self._get_result


class TestFailurePaths:
    """The never-crash guarantee: OS failures return False/None, never raise.

    These fake the kernel32 handle, so they run on any platform and do not
    touch the real thread priority.
    """

    def test_set_priority_os_refusal_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            thread_priority, "_kernel32", _FakeKernel32(set_result=0)
        )
        assert elevate_current_thread("highest") is False

    def test_set_priority_exception_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            thread_priority,
            "_kernel32",
            _FakeKernel32(set_raises=OSError("access violation")),
        )
        assert elevate_current_thread("time_critical") is False

    def test_get_priority_error_sentinel_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            thread_priority, "_kernel32", _FakeKernel32(get_result=0x7FFFFFFF)
        )
        assert get_current_thread_priority() is None

    def test_no_kernel32_elevate_returns_false(self, monkeypatch):
        monkeypatch.setattr(thread_priority, "_kernel32", None)
        assert elevate_current_thread("highest") is False

    def test_no_kernel32_get_priority_returns_none(self, monkeypatch):
        monkeypatch.setattr(thread_priority, "_kernel32", None)
        assert get_current_thread_priority() is None

    def test_invalid_level_still_raises_with_no_kernel32(self, monkeypatch):
        monkeypatch.setattr(thread_priority, "_kernel32", None)
        with pytest.raises(ValueError):
            elevate_current_thread("supersonic")
