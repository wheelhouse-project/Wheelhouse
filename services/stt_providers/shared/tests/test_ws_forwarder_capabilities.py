"""Tests for the WSForwarder capability handshake (wh-nvyh).

The forwarder announces the provider's capabilities as the FIRST frame
after every (re)connect. WheelHouse resets its per-stream emits_eos flag
whenever a new client becomes the active stream, so a once-per-process
declaration would be lost on reconnect -- the forwarder itself owns the
per-connection send.
"""
import asyncio
import json
import sys
import threading
import types
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_stt.ws_forwarder import WSForwarder


class _State:
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class _FakeWS:
    """Records every frame; behavior after the first send is configurable.

    ``after_first_send``:
      - "stop": set the forwarder's stop event (ends the test run)
      - "close": flip state to CLOSED (simulates a server-side close,
        which drives the forwarder's reconnect path)
    """

    def __init__(self, stop_evt: threading.Event, after_first_send: str):
        self.sent = []
        self._stop_evt = stop_evt
        self._after_first_send = after_first_send
        self.state = _State.OPEN

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))
        if len(self.sent) == 1:
            if self._after_first_send == "stop":
                self._stop_evt.set()
            elif self._after_first_send == "close":
                self.state = _State.CLOSED

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Park until cancelled by the forwarder's disconnect path.
        await asyncio.sleep(3600)


class _FakeConnectCM:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


def _install_fake_websockets(monkeypatch, connections):
    """sys.modules['websockets'] stub; each connect() pops the next fake ws."""
    mod = types.ModuleType("websockets")
    mod.State = _State

    def connect(uri, ping_interval=None):
        return _FakeConnectCM(connections.pop(0))

    mod.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", mod)


def _run_sender_loop(forwarder, timeout_s: float = 10.0):
    async def run():
        forwarder._queue = asyncio.Queue()
        # A queued transcript that must NOT beat capabilities onto the wire.
        await forwarder._queue.put(
            {"type": "stable", "text": "hello", "utterance_id": 1}
        )
        await asyncio.wait_for(forwarder._sender_loop(), timeout=timeout_s)

    asyncio.run(run())


def test_capabilities_is_the_first_frame_on_connect(monkeypatch):
    stop_evt = threading.Event()
    ws = _FakeWS(stop_evt, after_first_send="stop")
    _install_fake_websockets(monkeypatch, [ws])

    fwd = WSForwarder(
        "127.0.0.1",
        9999,
        threading.Event(),
        provider_name="google_stt",
        emits_eos=True,
    )
    fwd._stop_evt = stop_evt
    _run_sender_loop(fwd)

    assert ws.sent, "forwarder sent nothing"
    assert ws.sent[0] == {
        "type": "capabilities",
        "provider": "google_stt",
        "emits_eos": True,
    }


def test_capabilities_resent_on_reconnect(monkeypatch):
    """First connection closes right after the capabilities frame; the
    reconnect must lead with a fresh capabilities frame too."""
    stop_evt = threading.Event()
    first = _FakeWS(stop_evt, after_first_send="close")
    second = _FakeWS(stop_evt, after_first_send="stop")
    _install_fake_websockets(monkeypatch, [first, second])

    fwd = WSForwarder(
        "127.0.0.1",
        9999,
        threading.Event(),
        provider_name="google_stt",
        emits_eos=True,
    )
    fwd._stop_evt = stop_evt
    _run_sender_loop(fwd, timeout_s=15.0)

    assert first.sent and first.sent[0]["type"] == "capabilities"
    assert second.sent and second.sent[0]["type"] == "capabilities"
    assert second.sent[0]["emits_eos"] is True


def test_defaults_declare_no_eos(monkeypatch):
    """A forwarder constructed without the new kwargs still declares --
    with the safe defaults (empty provider name, emits_eos False)."""
    stop_evt = threading.Event()
    ws = _FakeWS(stop_evt, after_first_send="stop")
    _install_fake_websockets(monkeypatch, [ws])

    fwd = WSForwarder("127.0.0.1", 9999, threading.Event())
    fwd._stop_evt = stop_evt
    _run_sender_loop(fwd)

    assert ws.sent[0] == {
        "type": "capabilities",
        "provider": "",
        "emits_eos": False,
    }


_PROVIDERS_DIR = Path(__file__).parent.parent.parent


def test_provider_mains_declare_their_capabilities():
    """Source-level drift alarm: each provider main must pass its
    capability declaration to WSForwarder. Google is the only eos
    emitter today; the two local providers declare False explicitly."""
    google = (_PROVIDERS_DIR / "google_stt_server" / "main.py").read_text(
        encoding="utf-8"
    )
    distil = (_PROVIDERS_DIR / "distil_medium_en" / "main.py").read_text(
        encoding="utf-8"
    )
    sherpa = (
        _PROVIDERS_DIR / "sherpa_offline_parakeet_stt_server" / "main.py"
    ).read_text(encoding="utf-8")

    assert "emits_eos=True" in google
    assert 'provider_name="google_stt"' in google
    assert "emits_eos=False" in distil
    assert 'provider_name="distil_medium_en"' in distil
    assert "emits_eos=False" in sherpa
    assert 'provider_name="sherpa_offline_parakeet"' in sherpa


class _FailingSendWS(_FakeWS):
    """First send raises, simulating WheelHouse restarting inside the
    connect-to-capabilities window."""

    async def send(self, payload: str) -> None:
        raise ConnectionResetError("connection lost during capabilities send")


def test_capabilities_send_failure_reconnects_cleanly(monkeypatch):
    """reviewer_0 finding wh-nvyh.1.2: a capabilities send failure must not
    skip the listener-task cancel; the forwarder tears the connection down
    deterministically and the next connect declares again."""
    stop_evt = threading.Event()
    first = _FailingSendWS(stop_evt, after_first_send="close")
    second = _FakeWS(stop_evt, after_first_send="stop")
    _install_fake_websockets(monkeypatch, [first, second])

    fwd = WSForwarder(
        "127.0.0.1",
        9999,
        threading.Event(),
        provider_name="google_stt",
        emits_eos=True,
    )
    fwd._stop_evt = stop_evt
    _run_sender_loop(fwd, timeout_s=15.0)

    assert first.sent == [], "the failing connection recorded a frame"
    assert second.sent and second.sent[0] == {
        "type": "capabilities",
        "provider": "google_stt",
        "emits_eos": True,
    }, "reconnect after a failed capabilities send must declare again"
