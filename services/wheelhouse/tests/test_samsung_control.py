"""Samsung transport and brightness contracts; all TV traffic is mocked."""
import asyncio
import hashlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.wheelhouse.integrations import samsung_control as mod

CERT = b'test certificate'
PIN = hashlib.sha256(CERT).hexdigest()
IP = '192.0.2.10'


@pytest.mark.parametrize('value', [None, True, 3232235908, b'192.0.2.10'])
def test_address_must_be_a_literal_string(value):
    with pytest.raises(mod.SamsungError, match='invalid_address'):
        mod.validate_address(value)


class Response:
    status = 200
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()
    def getheader(self, name, default=None):
        return default
    def read1(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk


@pytest.fixture
def connection(monkeypatch):
    conn = Mock()
    conn.sock.getpeercert.return_value = CERT
    conn.getresponse.return_value = Response({'id': '1', 'result': {'backlight': 5}})
    monkeypatch.setattr(mod.http.client, 'HTTPSConnection', lambda *a, **k: conn)
    return conn


def test_rpc_reads_string_id_and_native_brightness(connection):
    client = mod.SamsungIPClient(IP, 'test', PIN)
    assert client.read_backlight() == 5
    sent = json.loads(connection.request.call_args.kwargs['body'])
    assert sent['method'] == 'backlightControl'
    assert sent['params'] == {'AccessToken': 'test'}
    connection.close.assert_called_once()


@pytest.mark.parametrize('code,reason', [(-32010, 'pairing_required'),
    (-32001, 'unsupported'), (-32601, 'unsupported'), (-32002, 'rejected'),
    (-32003, 'rejected')])
def test_http_success_does_not_hide_rpc_failure(connection, code, reason):
    connection.getresponse.return_value = Response({'id': '1', 'error': {'code': code}})
    with pytest.raises(mod.SamsungError) as caught:
        mod.SamsungIPClient(IP, 'test-token', PIN).write_backlight(6)
    assert caught.value.reason == reason


def test_certificate_change_sends_no_token(connection):
    connection.sock.getpeercert.return_value = b'other certificate'
    with pytest.raises(mod.SamsungError, match='identity_mismatch'):
        mod.SamsungIPClient(IP, 'test-token', PIN).read_backlight()
    connection.request.assert_not_called()


@pytest.mark.parametrize('value', [True, -1, 51, '5.5', None, {}, 'secret data'])
def test_invalid_brightness_rejected(connection, value):
    connection.getresponse.return_value = Response({'id': '1', 'result': {'backlight': value}})
    with pytest.raises(mod.SamsungError, match='invalid_response'):
        mod.SamsungIPClient(IP, 'test-token', PIN).read_backlight()


def test_wrong_response_id_rejected(connection):
    connection.getresponse.return_value = Response({'id': '9', 'result': {'backlight': 5}})
    with pytest.raises(mod.SamsungError, match='invalid_response'):
        mod.SamsungIPClient(IP, 'test-token', PIN).read_backlight()


def test_timeout_is_distinct_from_auth_failure(connection):
    connection.connect.side_effect = TimeoutError('must not leak a request')
    with pytest.raises(mod.SamsungError) as caught:
        mod.SamsungIPClient(IP, 'test-token', PIN).read_backlight()
    assert caught.value.reason == 'timeout'
    assert 'leak' not in str(caught.value)


def test_pairing_round_trip_is_encrypted_and_bound(tmp_path):
    record = mod.Pairing(IP, 'MRN65R95HAFXZA', hashlib.sha256(b'identity').hexdigest(), PIN, 'test-token')
    path = tmp_path / 'tv.dpapi'
    mod.save_pairing(path, record)
    assert b'test-token' not in path.read_bytes()
    assert mod.load_pairing(path, IP) == record
    with pytest.raises(mod.SamsungError, match='identity_mismatch'):
        mod.load_pairing(path, '192.0.2.11')
    with pytest.raises(FileExistsError):
        mod.save_pairing(path, record)


class FakeTV:
    def __init__(self, level=5):
        self.level = level
        self.writes = []
        self.error = None
        self.ignore_write = False
    def read_backlight(self):
        if self.error:
            raise mod.SamsungError(self.error)
        return self.level
    def write_backlight(self, value):
        self.writes.append(value)
        if not self.ignore_write:
            self.level = value


@pytest.fixture
def controller(monkeypatch, tmp_path):
    ctrl = mod.SamsungControl(IP, tmp_path / 'tv.dpapi')
    tv = FakeTV()
    monkeypatch.setattr(ctrl, '_client_for_target', lambda: tv)
    return ctrl, tv


@pytest.mark.asyncio
async def test_set_requires_actual_readback(controller):
    ctrl, tv = controller
    tv.ignore_write = True
    assert await ctrl.set_brightness(12) is False
    assert ctrl.last_error == 'readback_mismatch'
    tv.ignore_write = False
    assert await ctrl.set_brightness(12) is True
    assert await ctrl.get_brightness() == 12
    assert ctrl.last_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize('native,delta,final,overflow', [(49,10,100,8),(1,-10,0,-8),
    (0,-2,0,-2),(50,2,100,2),(0,-1,0,-1),(50,1,100,1),
    (5,1,12,0),(5,-1,8,0),(5,0,10,0)])
async def test_change_and_partial_overflow(controller, native, delta, final, overflow):
    ctrl, tv = controller
    tv.level = native
    result = await ctrl.change_brightness(delta)
    assert result.error is None
    assert result.level == final
    assert result.overflow == overflow
    assert tv.level * 2 == final


@pytest.mark.asyncio
async def test_offline_recovers_on_next_command(controller):
    ctrl, tv = controller
    tv.error = 'offline'
    assert await ctrl.get_brightness() is None
    assert ctrl.last_error == 'offline'
    tv.error = None
    assert (await ctrl.change_brightness(2)).level == 12
    assert ctrl.last_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize('delta', [3, -3, 5, -5])
async def test_opposite_odd_deltas_do_not_drift(controller, delta):
    ctrl, tv = controller
    before = tv.level
    first = await ctrl.change_brightness(delta)
    second = await ctrl.change_brightness(-delta)
    assert first.error is None and second.error is None
    assert first.overflow == second.overflow == 0
    assert tv.level == before


@pytest.mark.asyncio
async def test_concurrent_changes_are_serialized(controller):
    ctrl, tv = controller
    await asyncio.gather(*(ctrl.change_brightness(2) for _ in range(8)))
    assert tv.level == 13
    assert tv.writes == list(range(6, 14))


def test_identity_change_prevents_authenticated_request(monkeypatch, tmp_path):
    pairing = mod.Pairing(IP, 'MRN65R95HAFXZA', 'original', PIN, 'test-token')
    monkeypatch.setattr(mod, 'load_pairing', lambda *a: pairing)
    monkeypatch.setattr(mod, 'read_identity', lambda *a: ('MRN65R95HAFXZA', 'different'))
    factory = Mock()
    monkeypatch.setattr(mod, 'SamsungIPClient', factory)
    with pytest.raises(mod.SamsungError, match='identity_mismatch'):
        mod.SamsungControl(IP, tmp_path / 'tv.dpapi')._client_for_target()
    factory.assert_not_called()


@pytest.mark.parametrize('code', [[], {}, None, True, '-32010'])
def test_malformed_error_code_is_sanitized(connection, code):
    connection.getresponse.return_value = Response({'id': 1, 'error': {'code': code}})
    with pytest.raises(mod.SamsungError, match='invalid_response'):
        mod.SamsungIPClient(IP, 'test-token', PIN).read_backlight()


@pytest.mark.parametrize('status', [301, 302, 401, 403, 500])
def test_http_failure_never_follows_redirect(connection, status):
    connection.getresponse.return_value.status = status
    with pytest.raises(mod.SamsungError):
        mod.SamsungIPClient(IP, 'test-token', PIN).read_backlight()
    assert connection.request.call_count == 1


def test_oversized_body_is_rejected(connection):
    connection.getresponse.return_value.body = b' ' * (mod.MAX_BODY + 1)
    with pytest.raises(mod.SamsungError, match='invalid_response'):
        mod.SamsungIPClient(IP, 'test-token', PIN).read_backlight()


def test_relative_credential_path_rejected():
    with pytest.raises(mod.SamsungError, match='invalid_credential_path'):
        mod.SamsungControl(IP, 'tv.dpapi')


def test_slow_response_headers_are_interrupted_at_deadline(connection, monkeypatch):
    # Windows timer waits and monotonic clocks can have different granularity.
    # Prove classification uses the interrupt cause even before the clock advances.
    monkeypatch.setattr(mod, 'time', SimpleNamespace(monotonic=lambda: 100.0))
    interrupted = threading.Event()
    connection.sock.shutdown.side_effect = lambda how: interrupted.set()
    def slow_headers():
        assert interrupted.wait(2), 'Request deadline did not interrupt the socket'
        raise OSError('connection interrupted')
    connection.getresponse.side_effect = slow_headers
    with pytest.raises(mod.SamsungError, match='timeout'):
        mod.SamsungIPClient(IP, 'test-token', PIN, timeout=0.05).read_backlight()
    connection.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancellations', [1, 2])
async def test_cancellation_waits_for_inflight_write(controller, cancellations):
    ctrl, tv = controller
    entered = threading.Event()
    release = threading.Event()
    original_write = tv.write_backlight
    def held_write(value):
        entered.set()
        assert release.wait(2)
        original_write(value)
    tv.write_backlight = held_write
    task = asyncio.create_task(ctrl.change_brightness(2))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tv.level == 6


@pytest.mark.asyncio
async def test_shutdown_waits_for_write_and_readback_after_repeated_cancellation(controller):
    from services.wheelhouse.event_bus import EventBus
    from services.wheelhouse.events import HardwareBrightnessCommand
    from services.wheelhouse.plugins.base import PluginState
    from services.wheelhouse.plugins.samsung_plugin import SamsungPlugin

    ctrl, tv = controller
    plugin = SamsungPlugin()
    plugin._active = True
    plugin._control = ctrl
    plugin._event_bus = EventBus()
    await plugin.start()
    write_entered, write_release = threading.Event(), threading.Event()
    readback_entered, readback_release = threading.Event(), threading.Event()
    original_write, original_read = tv.write_backlight, tv.read_backlight

    def held_write(value):
        write_entered.set()
        assert write_release.wait(5)
        original_write(value)

    def held_read():
        if write_entered.is_set():
            readback_entered.set()
            assert readback_release.wait(5)
        return original_read()

    tv.write_backlight, tv.read_backlight = held_write, held_read
    command = asyncio.create_task(plugin._handle_brightness_command(HardwareBrightnessCommand(2)))
    stop = None
    try:
        assert await asyncio.to_thread(write_entered.wait, 5)
        for _ in range(2):
            command.cancel()
            await asyncio.sleep(0)
        stop = asyncio.create_task(plugin.stop())
        await asyncio.sleep(0)
        assert not command.done() and not stop.done()
        assert plugin.state == PluginState.STOPPING
        write_release.set()
        assert await asyncio.to_thread(readback_entered.wait, 5)
        assert not stop.done(), 'Shutdown must wait for readback as well as the setter'
    finally:
        write_release.set()
        readback_release.set()
        await asyncio.gather(command, return_exceptions=True)
        if stop is not None:
            await stop
        else:
            await plugin.stop()
    assert command.cancelled()
    assert tv.level == 6
    assert plugin.state == PluginState.STOPPED
