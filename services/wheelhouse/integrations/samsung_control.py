"""Direct Samsung IP Control with explicit pairing and verified brightness writes.

The R95H uses backlightControl (0-50); brightnessControl is shadow detail.
Pairing is an explicit setup operation. Runtime never displays pairing prompts.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import hashlib
import http.client
import ipaddress
import json
import math
from pathlib import Path
import re
import ssl
import socket
import threading
import time
from typing import Callable, TypeVar

from services.wheelhouse.integrations.display_control_base import DisplayControl

MAX_BODY = 65536
_T = TypeVar('_T')


class SamsungError(Exception):
    """A stable public reason; never includes request bodies or credentials."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def validate_address(address: str) -> str:
    if not isinstance(address, str):
        raise SamsungError('invalid_address')
    try:
        parsed = ipaddress.IPv4Address(address)
        if (not parsed.is_private or parsed.is_loopback or parsed.is_multicast
                or parsed.is_unspecified or parsed.is_link_local):
            raise ValueError
        return str(parsed)
    except (ValueError, TypeError):
        raise SamsungError('invalid_address') from None


@dataclass(frozen=True)
class Pairing:
    address: str
    model: str
    identity_hash: str
    certificate_sha256: str
    token: str = field(repr=False)


def save_pairing(path: Path, pairing: Pairing) -> None:
    """Save one encrypted record for the current Windows user; never overwrite."""
    import win32crypt
    data = json.dumps(asdict(pairing)).encode('utf-8')
    encrypted = win32crypt.CryptProtectData(data, 'WheelHouse Samsung pairing',
                                          None, None, None, 1)
    with Path(path).open('xb') as output:
        output.write(encrypted)


def load_pairing(path: Path, address: str) -> Pairing:
    try:
        import win32crypt
        with Path(path).open('rb') as source:
            data = source.read(MAX_BODY + 1)
        if len(data) > MAX_BODY:
            raise ValueError
        plaintext = win32crypt.CryptUnprotectData(data, None, None, None, 1)[1]
        record = Pairing(**json.loads(plaintext))
        if (not all(isinstance(v, str) and v for v in asdict(record).values())
                or not re.fullmatch(r'[0-9a-f]{64}', record.certificate_sha256)
                or not re.fullmatch(r'[0-9a-f]{64}', record.identity_hash)
                or len(record.token) > 8192):
            raise ValueError
    except Exception:
        raise SamsungError('pairing_required') from None
    if record.address != address:
        raise SamsungError('identity_mismatch')
    return record


def _read_json(response, sock, deadline: float):
    length = response.getheader('Content-Length')
    if length is not None:
        try:
            if not 0 <= int(length) <= MAX_BODY:
                raise ValueError
        except ValueError:
            raise SamsungError('invalid_response') from None
    chunks = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SamsungError('timeout')
        sock.settimeout(remaining)
        chunk = response.read1(min(4096, MAX_BODY + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_BODY:
            raise SamsungError('invalid_response')
    try:
        result = json.loads(chunks)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, RecursionError):
        raise SamsungError('invalid_response') from None


@dataclass
class _DeadlineGuard:
    timer: threading.Timer
    expired: threading.Event

    def cancel(self) -> None:
        self.timer.cancel()


def _start_deadline_guard(sock, deadline: float) -> _DeadlineGuard:
    """Interrupt even slowly arriving HTTP headers at the overall deadline."""
    expired = threading.Event()
    def interrupt():
        expired.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    guard = threading.Timer(max(0, deadline - time.monotonic()), interrupt)
    guard.daemon = True
    guard.start()
    return _DeadlineGuard(guard, expired)


def read_identity(address: str, timeout: float = 3) -> tuple[str, str]:
    """Read public identity without a token, to catch a reassigned network address."""
    address = validate_address(address)
    conn = http.client.HTTPConnection(address, 8001, timeout=timeout)
    deadline = time.monotonic() + timeout
    guard = None
    try:
        conn.connect()
        sock = conn.sock
        guard = _start_deadline_guard(sock, deadline)
        conn.request('GET', '/api/v2/', headers={'Accept': 'application/json'})
        response = conn.getresponse()
        if response.status != 200:
            raise SamsungError('identity_unavailable')
        body = _read_json(response, sock, deadline)
        device = body.get('device')
        if not isinstance(device, dict):
            raise SamsungError('invalid_response')
        model, identity = device.get('modelName'), body.get('id')
        if not all(isinstance(v, str) and 0 < len(v) <= 1024 for v in (model, identity)):
            raise SamsungError('invalid_response')
        return model, hashlib.sha256(identity.encode()).hexdigest()
    except SamsungError:
        raise
    except TimeoutError:
        raise SamsungError('timeout') from None
    except (OSError, http.client.HTTPException):
        if (guard is not None and guard.expired.is_set()) or time.monotonic() >= deadline:
            raise SamsungError('timeout') from None
        raise SamsungError('offline') from None
    finally:
        if guard is not None:
            guard.cancel()
        conn.close()


class SamsungIPClient:
    """One fixed local endpoint; no redirects, proxy, discovery or auth fallback."""
    def __init__(self, address: str, token: str, fingerprint: str | None, timeout: float = 3):
        self.address = validate_address(address)
        if not math.isfinite(timeout) or not 0 < timeout <= 55:
            raise SamsungError('invalid_timeout')
        if fingerprint is not None and not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
            raise SamsungError('identity_mismatch')
        self._token = token
        self.fingerprint = fingerprint
        self.timeout = timeout

    def _request(self, method: str, value: int | None = None) -> dict:
        if method not in ('createAccessToken', 'getDeviceInformation', 'backlightControl'):
            raise SamsungError('unsupported')
        pairing = method == 'createAccessToken'
        if not pairing and (not self._token or self.fingerprint is None):
            raise SamsungError('pairing_required')
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': method}
        if not pairing:
            payload['params'] = {'AccessToken': self._token}
            if value is not None:
                if method != 'backlightControl' or type(value) is not int or not 0 <= value <= 50:
                    raise SamsungError('invalid_brightness')
                payload['params']['backlight'] = value
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # TV certificate is pinned before sending credentials.
        conn = http.client.HTTPSConnection(self.address, 1516, timeout=self.timeout, context=context)
        deadline = time.monotonic() + self.timeout
        guard = None
        try:
            conn.connect()
            sock = conn.sock
            guard = _start_deadline_guard(sock, deadline)
            actual = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
            if self.fingerprint is not None and actual != self.fingerprint:
                raise SamsungError('identity_mismatch')
            conn.request('POST', '/', body=json.dumps(payload).encode(),
                         headers={'Accept': 'application/json', 'Content-Type': 'application/json'})
            response = conn.getresponse()
            if response.status in (401, 403):
                raise SamsungError('pairing_required')
            if response.status != 200:
                raise SamsungError('invalid_response')
            body = _read_json(response, sock, deadline)
            if type(body.get('id')) not in (int, str) or body['id'] not in (1, '1'):
                raise SamsungError('invalid_response')
            if 'error' in body:
                error = body['error']
                code = error.get('code') if isinstance(error, dict) else None
                if type(code) is not int:
                    raise SamsungError('invalid_response')
                reason = {-32010: 'pairing_required', -32001: 'unsupported',
                          -32601: 'unsupported'}.get(code, 'rejected')
                raise SamsungError(reason)
            result = body.get('result')
            if not isinstance(result, dict):
                raise SamsungError('invalid_response')
            if pairing:
                self.fingerprint = actual
            return result
        except SamsungError:
            raise
        except TimeoutError:
            raise SamsungError('timeout') from None
        except (OSError, http.client.HTTPException):
            if (guard is not None and guard.expired.is_set()) or time.monotonic() >= deadline:
                raise SamsungError('timeout') from None
            raise SamsungError('offline') from None
        finally:
            if guard is not None:
                guard.cancel()
            conn.close()

    def pair(self) -> str:
        token = self._request('createAccessToken').get('AccessToken')
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise SamsungError('pairing_required')
        self._token = token
        return token

    def read_backlight(self) -> int:
        value = self._request('backlightControl').get('backlight')
        if isinstance(value, str) and re.fullmatch(r'[0-9]{1,2}', value):
            value = int(value)
        if type(value) is not int or not 0 <= value <= 50:
            raise SamsungError('invalid_response')
        return value

    def write_backlight(self, value: int) -> None:
        self._request('backlightControl', value)


@dataclass(frozen=True)
class BrightnessChange:
    level: int | None = None
    overflow: int = 0
    error: str | None = None


class SamsungControl(DisplayControl):
    """Normalized 0-100 interface; serialized transactions verify actual TV state."""
    def __init__(self, ip_address: str, credential_file: Path, timeout: float = 3):
        self.address = validate_address(ip_address)
        self.credential_file = Path(credential_file)
        if not self.credential_file.is_absolute():
            raise SamsungError('invalid_credential_path')
        if not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise SamsungError('invalid_timeout')
        self.timeout = timeout
        self.last_error: str | None = None
        self._lock = threading.Lock()

    @property
    def brightness_range(self) -> tuple[int, int]:
        return 0, 50

    def _client_for_target(self) -> SamsungIPClient:
        pairing = load_pairing(self.credential_file, self.address)
        model, identity = read_identity(self.address, self.timeout)
        if model != pairing.model or identity != pairing.identity_hash:
            raise SamsungError('identity_mismatch')
        return SamsungIPClient(self.address, pairing.token, pairing.certificate_sha256, self.timeout)

    async def _run(self, operation: Callable[[], _T]) -> _T:
        def serialized():
            with self._lock:
                return operation()
        task = asyncio.create_task(asyncio.to_thread(serialized))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A thread cannot be cancelled. Do not release the caller's transaction
            # lock while a bounded write/readback could still be completing.
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.done():
                        break
                except Exception:
                    # Retrieve a worker failure, but preserve the caller's
                    # cancellation after the transaction has finished.
                    break
            raise

    async def get_brightness(self) -> int | None:
        def read():
            try:
                value = self._client_for_target().read_backlight() * 2
                self.last_error = None
                return value
            except SamsungError as error:
                self.last_error = error.reason
                return None
        return await self._run(read)

    def _change(self, delta: int | None = None, target: int | None = None) -> BrightnessChange:
        try:
            client = self._client_for_target()
            current = client.read_backlight() * 2
            effective = delta or 0
            if effective:
                # Quantize relative movement symmetrically so opposite odd
                # deltas restore the same level instead of drifting darker.
                magnitude = max(2, abs(effective) // 2 * 2)
                effective = magnitude if effective > 0 else -magnitude
            requested = current + effective if target is None else target
            bounded = max(0, min(100, requested))
            native = bounded // 2
            if native * 2 != current:
                client.write_backlight(native)
                actual = client.read_backlight() * 2
                if actual != native * 2:
                    raise SamsungError('readback_mismatch')
            else:
                actual = current
            self.last_error = None
            # Only a physical boundary produces software-dimming overflow;
            # quantization to native steps is not an overflow condition.
            original_request = current + (delta or 0)
            overflow = original_request - max(0, min(100, original_request)) if target is None else 0
            return BrightnessChange(actual, overflow)
        except SamsungError as error:
            self.last_error = error.reason
            return BrightnessChange(error=error.reason)

    async def change_brightness(self, delta: int) -> BrightnessChange:
        if type(delta) is not int:
            raise ValueError('Brightness delta must be an integer')
        return await self._run(lambda: self._change(delta=delta))

    async def set_brightness(self, level: int) -> bool | None:
        if type(level) is not int:
            raise ValueError('Brightness level must be an integer')
        result = await self._run(lambda: self._change(target=level))
        return self._success(result)

    async def adjust_brightness(self, delta: int) -> bool | None:
        return self._success(await self.change_brightness(delta))

    @staticmethod
    def _success(result: BrightnessChange) -> bool | None:
        if result.error in ('offline', 'timeout'):
            return None
        return result.error is None
