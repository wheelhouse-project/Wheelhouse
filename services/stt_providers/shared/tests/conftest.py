"""Shared-suite fixtures.

wh-stt-load-metrics.3: a capture backend's audio callback registers its own
thread with MMCSS on the first frame. In the running provider that thread is
a fresh capture thread per stream, and the backend reverts the registration
when the stream finishes. In the test process it is the pytest thread, the
same one for every test, and nothing reverts it.

Measured on this machine on 2026-09-03: one real registration with the task
"Pro Audio" raises the calling thread's priority from 0 to 15 and leaves it
there until the handle is reverted, and a SECOND registration on a thread
that already holds one returns a NULL handle with winerror 1552. So without
this fixture the first test to drive an unpatched callback would leave the
pytest thread running time-critical for the rest of the session, and every
later test would log a registration failure that describes the test process
rather than the code under test.

The default is a failed registration, which runs the same fallback path the
code took before MMCSS existed. Tests that care about either MMCSS branch
patch the same name themselves; a patch applied inside a test wins over this
one (see tests/test_capture_mmcss.py).

That fallback path reaches the same process-wide state by the other route:
it reads the thread priority and calls SetThreadPriority when the thread is
below time-critical. Neutralising only the MMCSS half therefore left the
door open -- a test that drove the callback without patching the priority
names itself still put the pytest thread at 15 (measured 2026-09-03,
wh-stt-load-metrics.3.1.2). Making the elevation a no-op takes the callback
down its "host-managed; no elevation needed" branch and changes nothing
outside the test process.

WinRTAudioCapture._capture_loop makes that registration on its polling
thread. In the test process the loop runs on the pytest thread, but it DOES
revert what it registered, so an uncovered WinRT test leaks nothing
permanently. What it still does is register MMCSS for real inside the test,
which makes a second registration from a nested loop return a NULL handle
with winerror 1552, and it takes the pytest thread to time-critical through
the fallback's SetThreadPriority, which nothing reverts. Both names are
neutralised here for that reason (wh-stt-load-metrics.3).

The fixture used to neutralise three more names on shared_audio.microphone,
whose MicrophoneStream ran the PortAudio capture path. That module was
deleted with the rest of that path (wh-portaudio-capture-removal), so WinRT
is the only backend left to neutralise.
"""
import pytest

from shared_audio.thread_priority import (
    AVRT_TASK_PRO_AUDIO,
    MmcssRegistration,
)

_UNAVAILABLE = MmcssRegistration(
    AVRT_TASK_PRO_AUDIO, None, "avrt.dll unavailable")


@pytest.fixture(autouse=True)
def no_real_mmcss_registration(monkeypatch):
    """Keep every test out of the process-wide scheduling state."""
    monkeypatch.setattr(
        "shared_audio.capture.winrt_capture.register_current_thread_mmcss",
        lambda *args, **kwargs: _UNAVAILABLE,
    )
    monkeypatch.setattr(
        "shared_audio.capture.winrt_capture.elevate_current_thread",
        lambda *args, **kwargs: True,
    )
