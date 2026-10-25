"""Detects an app that holds the default speaker in exclusive mode.

wh-kodi-playback-echo-stutter, stage 2. While an app (Kodi with WASAPI
exclusive output, for example) holds the default render endpoint in exclusive
mode, WheelHouse pauses all listening. This module is the detector: it
publishes SpeakerExclusiveStateChangedEvent when the answer changes, and
StateManager does the rest.

Two methods, both measured on the reference machine (probe results are on the
bead):

- Method B (the start). A shared-mode IAudioClient on the default endpoint is
  initialized and never started, with an IAudioSessionEvents sink registered on
  its session. When another app opens the endpoint in exclusive mode, Windows
  disconnects the session with reason 5 (DisconnectReasonExclusiveModeOverride).
  The callback arrives at once on a COM worker thread. The stream is dead
  afterwards: creating a new one fails with AUDCLNT_E_DEVICE_IN_USE until the
  speaker is free.
- Method A (the end, and the fallback for the start). A fresh shared-mode
  IAudioClient is initialized on the endpoint and released at once.
  AUDCLNT_E_DEVICE_IN_USE (0x8889000A) means an app holds the speaker. S_OK
  means it is free. It runs at every poll (two times each second) while the
  speaker is held or while method B has no stream, and every
  BACKUP_CHECK_SECONDS (4.5 s) otherwise: each run costs audiodg.exe CPU (boss
  ruling 00:20).

Only those two answers change the state. Every other result (another HRESULT,
another disconnect reason, no render endpoint, a COM failure, a backend that
does not start) leaves listening as it is and logs one WARNING per kind for the
life of the process (acceptance item 4). WheelHouse shipped without this pause,
so a detector that cannot answer must not stop WheelHouse from listening.

The detector never opens an endpoint in exclusive mode, never starts a stream
and never writes sound (acceptance item 5).

Threads (acceptance item 7). All COM work runs on one dedicated daemon thread,
the same choice as utils/audio_suppression_decision.py, section "WHY A DAEMON
THREAD" (wh-logic-exit-hang): a COM call that never returns must not hold up
WheelHouse exit, and a worker pool thread is joined at interpreter exit while a
daemon thread is not. stop() sets a flag and returns; it does not join. The
thread hands events back with loop.call_soon_threadsafe, and a closed loop is
not an error.
"""

from __future__ import annotations

import asyncio
import ctypes
import gc
import logging
import queue
import threading
import time
from ctypes import HRESULT, POINTER, Structure, byref, c_float, c_int, c_ushort, c_void_p, c_wchar_p
from ctypes import c_uint, c_uint64, c_longlong
from ctypes.wintypes import BOOL, DWORD, HANDLE, LPCWSTR
from typing import Any, Callable, Optional

import comtypes
import pythoncom
from comtypes import COMError, COMObject, GUID, IUnknown, STDMETHOD

from services.wheelhouse.events import SpeakerExclusiveStateChangedEvent

logger = logging.getLogger(__name__)

# Two polls per second. Each poll reads the default endpoint (a device change is
# followed within about half a second, acceptance item 6) and, while the speaker
# is held or method B has no stream, runs method A. A pause therefore ends
# within about half a second of the speaker being free (acceptance item 2:
# end <= 2 s). Method B reports a start at once, with no poll involved.
POLL_INTERVAL_SECONDS = 0.5
# While the speaker is free and method B holds a stream, method A runs only
# this often. The check runs at the first poll on or after its due time, so a
# start that method B misses is caught within this interval plus one poll
# interval: 4.5 + 0.5 = 5 s (acceptance item 2: start <= 5 s when only method A
# sees it). Boss ruling 00:20: method A at every poll cost audiodg.exe 1.5 to
# 1.7 percent of one core while nothing held the speaker, and the cost scales
# with the method A rate.
BACKUP_CHECK_SECONDS = 4.5
THREAD_NAME = "ExclusiveSpeakerMonitor"
# How long startup waits for the detector's first answer
# (wh-kodi-playback-echo-stutter.1.1). The wait ends earlier when the answer is
# ready; after this long, startup goes on without it and logs one WARNING.
FIRST_ANSWER_TIMEOUT_SECONDS = 2.0

# What StateManager prints in its INFO line ("detected by <method>").
METHOD_SHARED_CHECK = "shared-mode check"
METHOD_SESSION_DISCONNECT = "session disconnect"
METHOD_DEFAULT_CHANGE = "default speaker change"

E_DEVICE_IN_USE = 0x8889000A  # AUDCLNT_E_DEVICE_IN_USE
E_NOTFOUND = 0x80070490  # no default render endpoint
DISCONNECT_REASON_EXCLUSIVE_OVERRIDE = 5  # DisconnectReasonExclusiveModeOverride

CLSCTX_ALL = 23
AUDCLNT_SHAREMODE_SHARED = 0
EDATAFLOW_RENDER = 0
EROLE_CONSOLE = 0
STGM_READ = 0
VT_LPWSTR = 31
# The shared-mode buffer duration passed to Initialize, in 100 ns units (1 s).
_BUFFER_DURATION = 10_000_000


class BackendHResultError(Exception):
    """A COM call failed with a known HRESULT."""

    def __init__(self, hresult: int, message: str = "") -> None:
        self.hresult = hresult & 0xFFFFFFFF
        super().__init__(message or f"0x{self.hresult:08X}")


# ------------------------------------------------------------ COM declarations
# Interface vtables, in the order of the Windows SDK headers (mmdeviceapi.h,
# audioclient.h, audiopolicy.h, propsys.h). Copied from the probe that proved
# them on this machine (scripts/probe/exclusive_mode_probe.py). Slots that this
# module never calls are declared only because a vtable needs every slot.


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", DWORD)]


class PROPVARIANT(Structure):
    # 24 bytes on x64: vt, 3 reserved WORDs, then a 16-byte union.
    _fields_ = [
        ("vt", c_ushort),
        ("r1", c_ushort),
        ("r2", c_ushort),
        ("r3", c_ushort),
        ("u0", c_uint64),
        ("u1", c_uint64),
    ]


class WAVEFORMATEX(Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", c_ushort),
        ("nChannels", c_ushort),
        ("nSamplesPerSec", DWORD),
        ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", c_ushort),
        ("wBitsPerSample", c_ushort),
        ("cbSize", c_ushort),
    ]


PKEY_Device_FriendlyName = PROPERTYKEY(GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)
CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")


class IPropertyStore(IUnknown):
    _iid_ = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
    _methods_ = [
        STDMETHOD(HRESULT, "GetCount", [POINTER(DWORD)]),
        STDMETHOD(HRESULT, "GetAt", [DWORD, POINTER(PROPERTYKEY)]),
        STDMETHOD(HRESULT, "GetValue", [POINTER(PROPERTYKEY), POINTER(PROPVARIANT)]),
        STDMETHOD(HRESULT, "SetValue", [POINTER(PROPERTYKEY), POINTER(PROPVARIANT)]),
        STDMETHOD(HRESULT, "Commit", []),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        STDMETHOD(HRESULT, "Activate", [POINTER(GUID), DWORD, c_void_p, POINTER(POINTER(IUnknown))]),
        STDMETHOD(HRESULT, "OpenPropertyStore", [DWORD, POINTER(POINTER(IPropertyStore))]),
        STDMETHOD(HRESULT, "GetId", [POINTER(c_void_p)]),
        STDMETHOD(HRESULT, "GetState", [POINTER(DWORD)]),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        STDMETHOD(HRESULT, "EnumAudioEndpoints", [c_int, DWORD, POINTER(c_void_p)]),
        STDMETHOD(HRESULT, "GetDefaultAudioEndpoint", [c_int, c_int, POINTER(POINTER(IMMDevice))]),
        STDMETHOD(HRESULT, "GetDevice", [LPCWSTR, POINTER(POINTER(IMMDevice))]),
        STDMETHOD(HRESULT, "RegisterEndpointNotificationCallback", [c_void_p]),
        STDMETHOD(HRESULT, "UnregisterEndpointNotificationCallback", [c_void_p]),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    _methods_ = [
        STDMETHOD(HRESULT, "Initialize", [c_int, DWORD, c_longlong, c_longlong, c_void_p, c_void_p]),
        STDMETHOD(HRESULT, "GetBufferSize", [POINTER(c_uint)]),
        STDMETHOD(HRESULT, "GetStreamLatency", [POINTER(c_longlong)]),
        STDMETHOD(HRESULT, "GetCurrentPadding", [POINTER(c_uint)]),
        STDMETHOD(HRESULT, "IsFormatSupported", [c_int, c_void_p, POINTER(c_void_p)]),
        STDMETHOD(HRESULT, "GetMixFormat", [POINTER(c_void_p)]),
        STDMETHOD(HRESULT, "GetDevicePeriod", [POINTER(c_longlong), POINTER(c_longlong)]),
        # Vtable slots 7 to 9. This module never calls them: the detector's
        # streams are never run.
        STDMETHOD(HRESULT, "RunSlot", []),
        STDMETHOD(HRESULT, "HaltSlot", []),
        STDMETHOD(HRESULT, "ResetSlot", []),
        STDMETHOD(HRESULT, "SetEventHandle", [HANDLE]),
        STDMETHOD(HRESULT, "GetService", [POINTER(GUID), POINTER(POINTER(IUnknown))]),
    ]


class IAudioSessionEvents(IUnknown):
    _iid_ = GUID("{24918ACC-64B3-37C1-8CA9-74A66E9957A8}")
    _methods_ = [
        STDMETHOD(HRESULT, "OnDisplayNameChanged", [LPCWSTR, c_void_p]),
        STDMETHOD(HRESULT, "OnIconPathChanged", [LPCWSTR, c_void_p]),
        STDMETHOD(HRESULT, "OnSimpleVolumeChanged", [c_float, BOOL, c_void_p]),
        STDMETHOD(HRESULT, "OnChannelVolumeChanged", [DWORD, POINTER(c_float), DWORD, c_void_p]),
        STDMETHOD(HRESULT, "OnGroupingParamChanged", [c_void_p, c_void_p]),
        STDMETHOD(HRESULT, "OnStateChanged", [c_int]),
        STDMETHOD(HRESULT, "OnSessionDisconnected", [c_int]),
    ]


class IAudioSessionControl(IUnknown):
    _iid_ = GUID("{F4B1A599-7266-4319-A8CA-E70ACB11E8CD}")
    _methods_ = [
        STDMETHOD(HRESULT, "GetState", [POINTER(c_int)]),
        STDMETHOD(HRESULT, "GetDisplayName", [POINTER(c_void_p)]),
        STDMETHOD(HRESULT, "SetDisplayName", [LPCWSTR, c_void_p]),
        STDMETHOD(HRESULT, "GetIconPath", [POINTER(c_void_p)]),
        STDMETHOD(HRESULT, "SetIconPath", [LPCWSTR, c_void_p]),
        STDMETHOD(HRESULT, "GetGroupingParam", [POINTER(GUID)]),
        STDMETHOD(HRESULT, "SetGroupingParam", [c_void_p, c_void_p]),
        STDMETHOD(HRESULT, "RegisterAudioSessionNotification", [POINTER(IAudioSessionEvents)]),
        STDMETHOD(HRESULT, "UnregisterAudioSessionNotification", [POINTER(IAudioSessionEvents)]),
    ]


class _SessionEvents(COMObject):
    """IAudioSessionEvents sink. Runs on a COM worker thread.

    A comtypes method receives `this` after `self`. The sink does one thing:
    pass a disconnect reason to the callback, which only queues it. It never
    raises into Windows.
    """

    _com_interfaces_ = [IAudioSessionEvents]

    def __init__(self, on_disconnect: Callable[[int], None]) -> None:
        super().__init__()
        self._on_disconnect = on_disconnect

    def OnDisplayNameChanged(self, this, name, context):
        return 0

    def OnIconPathChanged(self, this, path, context):
        return 0

    def OnSimpleVolumeChanged(self, this, volume, mute, context):
        return 0

    def OnChannelVolumeChanged(self, this, count, volumes, changed, context):
        return 0

    def OnGroupingParamChanged(self, this, param, context):
        return 0

    def OnStateChanged(self, this, state):
        return 0

    def OnSessionDisconnected(self, this, reason):
        try:
            self._on_disconnect(reason)
        except Exception:  # noqa: BLE001 -- nothing may propagate into Windows
            pass
        return 0


_ole32 = ctypes.windll.ole32
_ole32.CoTaskMemFree.argtypes = [c_void_p]
_ole32.CoTaskMemFree.restype = None
_ole32.PropVariantClear.argtypes = [POINTER(PROPVARIANT)]
_ole32.PropVariantClear.restype = HRESULT

_COM_ERRORS = (COMError, OSError)


def _hresult_of(exc: BaseException) -> int:
    """The HRESULT a COM exception carries; E_FAIL when it carries none."""
    hr = getattr(exc, "hresult", None)
    if hr is None:
        hr = getattr(exc, "winerror", None)
    if hr is None and getattr(exc, "args", None) and isinstance(exc.args[0], int):
        hr = exc.args[0]
    return (hr if hr is not None else 0x80004005) & 0xFFFFFFFF


def _activate(device, interface):
    punk = POINTER(IUnknown)()
    device.Activate(byref(interface._iid_), CLSCTX_ALL, None, byref(punk))
    return punk.QueryInterface(interface)


def _mix_format(client) -> bytes:
    pointer = c_void_p()
    client.GetMixFormat(byref(pointer))
    try:
        header = WAVEFORMATEX.from_address(pointer.value)
        return ctypes.string_at(pointer.value, ctypes.sizeof(WAVEFORMATEX) + header.cbSize)
    finally:
        _ole32.CoTaskMemFree(pointer)


def _initialize_shared(client) -> None:
    """Initialize `client` in shared mode with the mix format. Never exclusive."""
    fmt = _mix_format(client)
    buffer = ctypes.create_string_buffer(fmt, len(fmt))
    client.Initialize(AUDCLNT_SHAREMODE_SHARED, 0, _BUFFER_DURATION, 0, buffer, None)


class _HeldStream:
    """One shared-mode stream, initialized and held open. Never started."""

    def __init__(self, device, on_disconnect: Callable[[int], None]) -> None:
        self.client = _activate(device, IAudioClient)
        _initialize_shared(self.client)
        punk = POINTER(IUnknown)()
        self.client.GetService(byref(IAudioSessionControl._iid_), byref(punk))
        self.control = punk.QueryInterface(IAudioSessionControl)
        self.sink = _SessionEvents(on_disconnect)
        self.sink_pointer = self.sink.QueryInterface(IAudioSessionEvents)
        self.control.RegisterAudioSessionNotification(self.sink_pointer)

    def close(self) -> None:
        try:
            self.control.UnregisterAudioSessionNotification(self.sink_pointer)
        except _COM_ERRORS:
            pass
        self.sink_pointer = None
        self.sink = None
        self.control = None
        self.client = None


class WasapiBackend:
    """The real COM layer. Every method runs on the detector thread."""

    def __init__(self) -> None:
        self._enumerator = None
        self._com_initialized = False

    def initialize(self) -> None:
        # The multithreaded apartment is what lets Windows deliver the session
        # callback on its own worker thread with no message pump (measured).
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        self._com_initialized = True
        try:
            self._enumerator = comtypes.CoCreateInstance(
                CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL
            )
        except BaseException:
            self.shutdown()
            raise

    def shutdown(self) -> None:
        self._enumerator = None
        gc.collect()  # release every COM pointer before the apartment goes
        if self._com_initialized:
            self._com_initialized = False
            pythoncom.CoUninitialize()

    def default_endpoint(self) -> Optional[tuple[str, str]]:
        """(id, friendly name) of the default render endpoint, or None."""
        device = POINTER(IMMDevice)()
        try:
            self._enumerator.GetDefaultAudioEndpoint(EDATAFLOW_RENDER, EROLE_CONSOLE, byref(device))
        except _COM_ERRORS as exc:
            if _hresult_of(exc) == E_NOTFOUND:
                return None
            raise
        return self._device_id(device), self._friendly_name(device)

    def check_in_use(self, endpoint_id: str) -> int:
        """Method A: a fresh shared-mode Initialize. Returns its HRESULT."""
        try:
            device = self._device(endpoint_id)
            client = _activate(device, IAudioClient)
            _initialize_shared(client)
        except _COM_ERRORS as exc:
            return _hresult_of(exc)
        finally:
            client = None  # noqa: F841 -- releases the IAudioClient at once
        return 0

    def open_held_stream(self, endpoint_id: str, on_disconnect: Callable[[int], None]):
        """Method B: hold a shared-mode stream. Raises BackendHResultError."""
        try:
            return _HeldStream(self._device(endpoint_id), on_disconnect)
        except _COM_ERRORS as exc:
            raise BackendHResultError(_hresult_of(exc)) from exc

    def close_held_stream(self, handle) -> None:
        handle.close()

    def _device(self, endpoint_id: str):
        device = POINTER(IMMDevice)()
        self._enumerator.GetDevice(endpoint_id, byref(device))
        return device

    @staticmethod
    def _device_id(device) -> str:
        pointer = c_void_p()
        device.GetId(byref(pointer))
        try:
            return c_wchar_p(pointer.value).value
        finally:
            _ole32.CoTaskMemFree(pointer)

    @staticmethod
    def _friendly_name(device) -> str:
        try:
            store = POINTER(IPropertyStore)()
            device.OpenPropertyStore(STGM_READ, byref(store))
            value = PROPVARIANT()
            store.GetValue(byref(PKEY_Device_FriendlyName), byref(value))
            try:
                if value.vt == VT_LPWSTR and value.u0:
                    return c_wchar_p(value.u0).value or "(unnamed speaker)"
            finally:
                _ole32.PropVariantClear(byref(value))
        except _COM_ERRORS:
            pass
        return "(unnamed speaker)"


# ------------------------------------------------------------------- monitor


class ExclusiveSpeakerMonitor:
    """Publishes SpeakerExclusiveStateChangedEvent when the answer changes.

    Args:
        loop: the asyncio loop events are handed back to.
        event_bus: where events are published (on the loop).
        backend_factory: builds the COM layer on the detector thread; replaced
            in tests.
        poll_interval: seconds between polls. A poll reads the default endpoint
            and, when method A is due, runs it.
        backup_interval: seconds between method A runs while the speaker is
            free and method B holds a stream.

    State (``_held``, the endpoint, the held stream, the next backup time, the
    warned kinds) is read
    and written only on the detector thread. The only cross-thread paths are
    the notice queue (COM worker thread to detector thread), stop() (any
    thread to the detector thread) and the loop hand-back.
    """

    def __init__(
        self,
        loop,
        event_bus,
        backend_factory: Callable[[], Any] = WasapiBackend,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        backup_interval: float = BACKUP_CHECK_SECONDS,
    ) -> None:
        self._loop = loop
        self._event_bus = event_bus
        self._backend_factory = backend_factory
        self.poll_interval = poll_interval
        self.backup_interval = backup_interval
        self._backend = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._notices: queue.SimpleQueue = queue.SimpleQueue()
        self._publish_tasks: set = set()
        # Set on the loop, by a call_soon_threadsafe callback of the detector
        # thread, when the first answer is ready (see wait_first_answer).
        self._first_answer = asyncio.Event()
        # Detector-thread state.
        self._first_answer_signalled = False
        self._held = False
        self._endpoint_id: Optional[str] = None
        self._endpoint_name = ""
        self._stream = None
        self._stream_gen = 0
        # time.monotonic() value from which a backup method A run is due. 0.0
        # makes the first poll run method A.
        self._next_backup_check = 0.0
        self._warned: set[str] = set()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the detector thread. Does nothing when it already runs."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
        self._thread.start()

    async def wait_first_answer(self, timeout: float = FIRST_ANSWER_TIMEOUT_SECONDS) -> bool:
        """Wait, on the loop, until StateManager can already know the first answer.

        Ready means either (a) the first poll finished and every event it
        published has finished delivery to the subscribers on the event bus, or
        (b) the backend did not start. Returns True when ready. After
        ``timeout`` seconds it logs one WARNING and returns False, and the
        caller goes on without the answer.

        Call it on the loop thread, after start(). It blocks nothing: it awaits
        an asyncio.Event, then the publish tasks, and it adds no thread.

        The detector thread signals through call_soon_threadsafe, and the loop
        runs those callbacks in the order they were queued. The callback that
        creates the publish task for a held event is queued before the one that
        sets the event, so the task is in ``_publish_tasks`` by the time this
        coroutine wakes.
        """
        async def _ready() -> None:
            await self._first_answer.wait()
            pending = list(self._publish_tasks)
            if pending:
                # asyncio.wait does not cancel the tasks when this wait times
                # out: a delivery that has begun must finish.
                await asyncio.wait(pending)

        try:
            await asyncio.wait_for(_ready(), timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "[EXCLUSIVE SPEAKER] The exclusive-mode detector gave no answer "
                "within %.1f s; startup goes on without it, and the first answer "
                "reaches listening when it arrives.",
                timeout,
            )
            return False
        return True

    def stop(self) -> None:
        """Ask the detector thread to end. Returns at once; never joins.

        A COM call that never returns must not hold up WheelHouse exit, and
        the thread is a daemon thread, so the interpreter leaves without it.
        """
        self._stop.set()
        self._wake.set()

    # --------------------------------------------------------------- thread

    def _run(self) -> None:
        # crewcut: a failed backend start ends this thread. main starts the
        # detector early, and start_services calls start() again, so a
        # failed start gets one more try only if this thread has already
        # ended by then. After that the detector stays off until WheelHouse
        # restarts. The effect equals the shipped behavior before this
        # detector (no exclusive-mode pause). To remove the limit, retry
        # initialize() on a back-off inside the loop below.
        started = False
        try:
            if self._backend is None:
                self._backend = self._backend_factory()
            self._backend.initialize()
            started = True
        except Exception as exc:  # noqa: BLE001 -- every failure leaves listening as it is
            self._warn_once(
                "start",
                "[EXCLUSIVE SPEAKER] The exclusive-mode detector did not start "
                "(%s: %s). Listening is not paused for exclusive-mode apps.",
                type(exc).__name__,
                exc,
            )
            # Nothing will ever answer; startup must not wait for it.
            self._signal_first_answer()
            return
        logger.info("[EXCLUSIVE SPEAKER] Exclusive-mode detector started")
        try:
            next_poll = time.monotonic()
            while not self._stop.is_set():
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + self.poll_interval
                    self._guarded(self._poll_once)
                    self._signal_first_answer()
                else:
                    self._guarded(self._drain_notifications)
                self._wake.wait(max(0.0, next_poll - time.monotonic()))
                self._wake.clear()
        finally:
            if started:
                self._guarded(self._release_stream)
                self._guarded(self._backend.shutdown)

    def _signal_first_answer(self) -> None:
        """Tell the loop the first answer is ready. Detector thread; once.

        Called after the first poll and on a failed start. The event the poll
        published is already queued on the loop by then (see wait_first_answer).
        """
        if self._first_answer_signalled:
            return
        self._first_answer_signalled = True
        try:
            self._loop.call_soon_threadsafe(self._first_answer.set)
        except RuntimeError:
            # The loop closed while the detector ran. Nobody is waiting.
            pass

    def _guarded(self, step: Callable[[], None]) -> None:
        """Run one step; an unexpected error must not end the thread."""
        try:
            step()
        except Exception as exc:  # noqa: BLE001
            self._warn_once(
                f"step-error:{type(exc).__name__}",
                "[EXCLUSIVE SPEAKER] Unexpected %s in the exclusive-mode detector; "
                "listening is left as it is: %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )

    # ---------------------------------------------------------------- checks

    def _poll_once(self) -> None:
        """One round: queued notices, the default endpoint, method A when due, method B's stream.

        Method A runs when (a) the speaker is held, (b) method B has no stream
        (the endpoint is new, the stream died or was refused), or (c) the
        backup time has come. Method A runs before _ensure_stream, so a free
        answer after a held period opens the new stream in the same poll.
        """
        self._drain_notifications()
        endpoint = self._read_endpoint()
        if endpoint is None:
            return
        self._follow_endpoint(*endpoint)
        now = time.monotonic()
        if self._held or self._stream is None or now >= self._next_backup_check:
            self._next_backup_check = now + self.backup_interval
            self._shared_mode_check()
        self._ensure_stream()

    def _read_endpoint(self) -> Optional[tuple[str, str]]:
        try:
            endpoint = self._backend.default_endpoint()
        except Exception as exc:  # noqa: BLE001
            self._warn_once(
                f"endpoint-error:{type(exc).__name__}",
                "[EXCLUSIVE SPEAKER] Could not read the default speaker (%s: %s); "
                "listening is left as it is.",
                type(exc).__name__,
                exc,
            )
            return None
        if endpoint is None:
            self._warn_once(
                "endpoint-none",
                "[EXCLUSIVE SPEAKER] Windows names no default speaker; "
                "listening is left as it is.",
            )
            self._drop_endpoint()
        return endpoint

    def _drop_endpoint(self) -> None:
        """No default speaker: end a pause the old one caused (acceptance item 6)."""
        old_name = self._endpoint_name
        self._release_stream()
        self._endpoint_id = None
        if self._held:
            self._set_held(False, old_name, METHOD_DEFAULT_CHANGE)

    def _follow_endpoint(self, endpoint_id: str, name: str) -> None:
        """Move to the new default speaker and end a pause the old one caused."""
        changed = self._endpoint_id is not None and endpoint_id != self._endpoint_id
        old_name = self._endpoint_name
        if changed:
            self._release_stream()
        self._endpoint_id = endpoint_id
        self._endpoint_name = name
        if changed and self._held:
            self._set_held(False, old_name, METHOD_DEFAULT_CHANGE)

    def _shared_mode_check(self) -> None:
        """Method A."""
        try:
            hresult = self._backend.check_in_use(self._endpoint_id)
        except Exception as exc:  # noqa: BLE001
            self._warn_once(
                f"check-error:{type(exc).__name__}",
                "[EXCLUSIVE SPEAKER] The shared-mode check on '%s' failed (%s: %s); "
                "listening is left as it is.",
                self._endpoint_name,
                type(exc).__name__,
                exc,
            )
            return
        hresult &= 0xFFFFFFFF
        if hresult == E_DEVICE_IN_USE:
            # A held stream that did not report this start cannot be trusted to
            # report the next one; release it so a fresh one opens when free.
            self._release_stream()
            self._set_held(True, self._endpoint_name, METHOD_SHARED_CHECK)
        elif hresult == 0:
            self._set_held(False, self._endpoint_name, METHOD_SHARED_CHECK)
        else:
            self._warn_once(
                f"check-hresult:0x{hresult:08X}",
                "[EXCLUSIVE SPEAKER] The shared-mode check on '%s' answered 0x%08X, "
                "which is neither free nor in use; listening is left as it is.",
                self._endpoint_name,
                hresult,
            )

    def _ensure_stream(self) -> None:
        """Method B: hold a stream on the endpoint, creating it when none is held."""
        if self._stream is not None:
            return
        self._stream_gen += 1
        generation = self._stream_gen
        try:
            self._stream = self._backend.open_held_stream(
                self._endpoint_id, lambda reason, g=generation: self._notify(g, reason)
            )
        except BackendHResultError as exc:
            if exc.hresult == E_DEVICE_IN_USE:
                return  # expected while an app holds the speaker
            self._warn_once(
                f"stream-hresult:0x{exc.hresult:08X}",
                "[EXCLUSIVE SPEAKER] Could not hold a shared-mode stream on '%s' "
                "(0x%08X); only the shared-mode check detects exclusive mode "
                "(it then runs at every poll, two times each second).",
                self._endpoint_name,
                exc.hresult,
            )
        except Exception as exc:  # noqa: BLE001
            self._warn_once(
                f"stream-error:{type(exc).__name__}",
                "[EXCLUSIVE SPEAKER] Could not hold a shared-mode stream on '%s' "
                "(%s: %s); only the shared-mode check detects exclusive mode "
                "(it then runs at every poll, two times each second).",
                self._endpoint_name,
                type(exc).__name__,
                exc,
            )

    def _release_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            self._backend.close_held_stream(stream)
        except Exception as exc:  # noqa: BLE001
            self._warn_once(
                f"close-error:{type(exc).__name__}",
                "[EXCLUSIVE SPEAKER] Could not release the held stream (%s: %s).",
                type(exc).__name__,
                exc,
            )

    # ------------------------------------------------------ method B notices

    def _notify(self, generation: int, reason: Any) -> None:
        """Runs on a COM worker thread: queue the notice and wake the detector."""
        self._notices.put((generation, reason))
        self._wake.set()

    def _drain_notifications(self) -> None:
        while True:
            try:
                generation, reason = self._notices.get_nowait()
            except queue.Empty:
                return
            if self._stream is None or generation != self._stream_gen:
                continue  # from a stream that was already replaced
            self._release_stream()  # the session is over; a new stream follows
            if reason == DISCONNECT_REASON_EXCLUSIVE_OVERRIDE:
                self._set_held(True, self._endpoint_name, METHOD_SESSION_DISCONNECT)
            else:
                self._warn_once(
                    f"disconnect:{reason}",
                    "[EXCLUSIVE SPEAKER] The held stream on '%s' ended with session "
                    "disconnect reason %s, which is not an exclusive-mode override; "
                    "listening is left as it is.",
                    self._endpoint_name,
                    reason,
                )

    # --------------------------------------------------------------- results

    def _set_held(self, held: bool, endpoint_name: str, method: str) -> None:
        """Record a change of state and publish it. No change, no event."""
        if held == self._held or self._stop.is_set():
            return
        self._held = held
        event = SpeakerExclusiveStateChangedEvent(
            held=held, endpoint_name=endpoint_name, method=method
        )
        try:
            self._loop.call_soon_threadsafe(self._publish_on_loop, event)
        except RuntimeError:
            # The loop closed while the detector ran. Nothing is listening.
            pass

    def _publish_on_loop(self, event: SpeakerExclusiveStateChangedEvent) -> None:
        task = self._loop.create_task(self._event_bus.publish(event))
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    def _warn_once(self, kind: str, message: str, *args: Any, exc_info: bool = False) -> None:
        """One WARNING per kind of failure for the life of the process."""
        if kind in self._warned:
            return
        self._warned.add(kind)
        logger.warning(message, *args, exc_info=exc_info)
