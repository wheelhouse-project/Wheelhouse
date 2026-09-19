"""Tests for the two MMCSS helpers in shared_audio/thread_priority.py.

wh-stt-load-metrics.3. Measured on Ikon 2026-09-03: an independent WASAPI
capture from the same Scarlett Solo had 0 overflows in 62 ten-second buckets
while the Parakeet provider lost up to 60 frames per minute over the same
minutes with the disk idle, so the loss is inside the provider process and the
capture thread is the part that is not being scheduled on time.

SetThreadPriority raises a thread inside the ordinary scheduler. MMCSS
(AvSetMmThreadCharacteristicsW, task "Pro Audio") moves it onto the
multimedia scheduling path that the Windows audio engine itself uses. The
registration is per-thread and must run on the thread it protects.

This file used to carry a second half: sixteen tests driving
MicrophoneStream._callback, which registered from inside the PortAudio
callback on its first invocation. That class was deleted with the PortAudio
capture path (wh-portaudio-capture-removal). WinRTAudioCapture makes the
same registration on its poll loop, and tests/test_winrt_capture.py covers
that half.

The Win32 calls are faked here, so every branch runs on any machine and no
test changes the real scheduling of the pytest process.
"""
import ctypes

import pytest

import shared_audio.thread_priority as thread_priority
from shared_audio.thread_priority import (
    AVRT_TASK_PRO_AUDIO,
    register_current_thread_mmcss,
    revert_current_thread_mmcss,
)


class _FakeAvrt:
    """Stand-in for the avrt.dll handle so every branch runs anywhere."""

    def __init__(self, set_result: object = 0xABCD, set_raises=None,
                 revert_result=1, revert_raises=None):
        self._set_result = set_result
        self._set_raises = set_raises
        self._revert_result = revert_result
        self._revert_raises = revert_raises
        self.set_calls = []
        self.revert_calls = []

    def AvSetMmThreadCharacteristicsW(self, task, index_ref):
        if self._set_raises is not None:
            raise self._set_raises
        # PortAudio's own docs require the task index to start at zero; the
        # test records what the caller passed so a future edit cannot drop it.
        self.set_calls.append((task, index_ref._obj.value))
        return self._set_result

    def AvRevertMmThreadCharacteristics(self, handle):
        if self._revert_raises is not None:
            raise self._revert_raises
        self.revert_calls.append(handle)
        return self._revert_result


class TestRegisterCurrentThreadMmcss:
    def test_success_returns_handle_and_task(self, monkeypatch):
        fake = _FakeAvrt(set_result=0xABCD)
        monkeypatch.setattr(thread_priority, "_avrt", fake)
        result = register_current_thread_mmcss()
        assert result.handle == 0xABCD
        assert result.task == AVRT_TASK_PRO_AUDIO
        assert result.error is None

    def test_task_index_starts_at_zero(self, monkeypatch):
        """AvSetMmThreadCharacteristicsW requires a zeroed task index on the
        first call for a thread; a stale value makes the call fail."""
        fake = _FakeAvrt()
        monkeypatch.setattr(thread_priority, "_avrt", fake)
        register_current_thread_mmcss()
        assert fake.set_calls == [(AVRT_TASK_PRO_AUDIO, 0)]

    def test_caller_may_choose_the_task_name(self, monkeypatch):
        fake = _FakeAvrt()
        monkeypatch.setattr(thread_priority, "_avrt", fake)
        result = register_current_thread_mmcss("Audio")
        assert fake.set_calls == [("Audio", 0)]
        assert result.task == "Audio"

    def test_null_handle_reports_the_winerror(self, monkeypatch):
        """A NULL return is the documented failure; ctypes gives None for a
        c_void_p NULL, so the check must not be a plain 'is not None'."""
        monkeypatch.setattr(
            thread_priority, "_avrt", _FakeAvrt(set_result=None))
        ctypes.set_last_error(87)  # ERROR_INVALID_PARAMETER
        result = register_current_thread_mmcss()
        assert result.handle is None
        assert result.error == "winerror 87"

    def test_zero_handle_is_also_a_failure(self, monkeypatch):
        monkeypatch.setattr(thread_priority, "_avrt", _FakeAvrt(set_result=0))
        result = register_current_thread_mmcss()
        assert result.handle is None
        assert result.error is not None

    def test_exception_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(
            thread_priority, "_avrt",
            _FakeAvrt(set_raises=OSError("avrt exploded")))
        result = register_current_thread_mmcss()
        assert result.handle is None
        assert result.error is not None and "avrt exploded" in result.error

    def test_no_avrt_reports_unavailable(self, monkeypatch):
        monkeypatch.setattr(thread_priority, "_avrt", None)
        result = register_current_thread_mmcss()
        assert result.handle is None
        assert result.error == "avrt.dll unavailable"


class TestRevertCurrentThreadMmcss:
    def test_success_passes_the_handle_back(self, monkeypatch):
        fake = _FakeAvrt()
        monkeypatch.setattr(thread_priority, "_avrt", fake)
        assert revert_current_thread_mmcss(0xABCD) is True
        assert fake.revert_calls == [0xABCD]

    def test_os_refusal_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            thread_priority, "_avrt", _FakeAvrt(revert_result=0))
        assert revert_current_thread_mmcss(0xABCD) is False

    def test_exception_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            thread_priority, "_avrt",
            _FakeAvrt(revert_raises=OSError("bad handle")))
        assert revert_current_thread_mmcss(0xABCD) is False

    def test_no_avrt_returns_false(self, monkeypatch):
        monkeypatch.setattr(thread_priority, "_avrt", None)
        assert revert_current_thread_mmcss(0xABCD) is False

