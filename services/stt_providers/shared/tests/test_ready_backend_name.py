"""The ready notification says which capture path the provider took.

wh-capture-winrt-required criterion A4. The bug this bead exists for was
silent: the Parakeet provider that started at 15:25 on 2026-09-05
captured through PortAudio for its whole run because winsdk was missing
from its venv, and nothing in any log said so. The factory now refuses
instead of choosing a second path, but a refusal only covers the case
where winsdk is absent. Naming the path in the ready notification is
what makes a WRONG path visible rather than silent, which is the half
the refusal cannot cover.

The name lives in the factory, beside the one path it can return, so a
provider cannot report a backend the factory never built.
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest
import websockets

from shared_audio.capture import CAPTURE_BACKEND_NAME
from shared_audio.capture import factory
from shared_stt.ws_forwarder import WSForwarder


def test_the_backend_name_is_winrt():
    """Spelled out rather than compared to itself. WheelHouse writes
    this string into wheelhouse.log, so a changed value is a changed log
    line that an operator's own search would stop matching."""
    assert CAPTURE_BACKEND_NAME == "winrt"


def test_the_name_belongs_to_the_factory_that_builds_the_capture():
    """Exported from the package, defined in the factory. A provider
    that read the name from anywhere else could report a path the
    factory did not build."""
    assert factory.CAPTURE_BACKEND_NAME == CAPTURE_BACKEND_NAME


class TestTheNotificationCarriesTheName:
    """Over a real socket, because the field has to survive the frame.

    A test against a stubbed forwarder proves the argument reached
    send_notification and nothing about what WheelHouse receives, and
    WheelHouse is the only reader of this field.
    """

    @pytest.mark.asyncio
    async def test_a_ready_notification_carries_the_capture_backend(self):
        received = []
        connected = asyncio.Event()

        async def handler(websocket):
            await websocket.send(
                '{"type": "status", "transcription_enabled": true}')
            connected.set()
            try:
                async for message in websocket:
                    frame = json.loads(message)
                    if frame.get("type") != "capabilities":
                        received.append(frame)
            except Exception:
                pass

        # Port 0 lets the operating system pick a free port and hand it
        # back. A fixed number would collide with another suite: three
        # worktrees run tests at the same time on this machine.
        #
        # The address is 127.0.0.1, not "localhost", and that is
        # load-bearing with port 0. "localhost" resolves to both an IPv4
        # and an IPv6 address, websockets binds a socket to each, and
        # with port 0 the operating system gives the two sockets
        # DIFFERENT ephemeral ports. sockets[0] then names one of them
        # while the client dials the other, and the connection never
        # arrives. A fixed port hid this because both sockets carried the
        # same number.
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        event = threading.Event()
        event.set()
        forwarder = WSForwarder(
            host="127.0.0.1", port=port,
            transcription_enabled_event=event, debug=False)
        forwarder.start()
        try:
            await asyncio.wait_for(connected.wait(), timeout=2.0)
            await asyncio.sleep(0.1)
            forwarder.send_notification(
                "Parakeet v3 (CPU)", "Transcription service ready",
                kind="ready", capture_backend=CAPTURE_BACKEND_NAME)
            await asyncio.sleep(0.5)
        finally:
            forwarder.stop()
            server.close()
            await server.wait_closed()

        assert received, "no notification frame arrived"
        assert received[0]["kind"] == "ready"
        assert received[0]["capture_backend"] == "winrt"

    @pytest.mark.asyncio
    async def test_a_notice_that_names_no_backend_still_carries_the_field(self):
        """Empty, not absent. WheelHouse reads the field on every
        notification, and an absent key would make a plain notice and a
        ready notice two different shapes to read."""
        received = []
        connected = asyncio.Event()

        async def handler(websocket):
            await websocket.send(
                '{"type": "status", "transcription_enabled": true}')
            connected.set()
            try:
                async for message in websocket:
                    frame = json.loads(message)
                    if frame.get("type") != "capabilities":
                        received.append(frame)
            except Exception:
                pass

        # Port 0 and 127.0.0.1 for the same reasons as the test above: a
        # fixed number collides with a suite running in another worktree,
        # and "localhost" with port 0 gives the IPv4 and IPv6 sockets two
        # different ports.
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        event = threading.Event()
        event.set()
        forwarder = WSForwarder(
            host="127.0.0.1", port=port,
            transcription_enabled_event=event, debug=False)
        forwarder.start()
        try:
            await asyncio.wait_for(connected.wait(), timeout=2.0)
            await asyncio.sleep(0.1)
            forwarder.send_notification("Parakeet v3 (CPU)", "Hello")
            await asyncio.sleep(0.5)
        finally:
            forwarder.stop()
            server.close()
            await server.wait_closed()

        assert received, "no notification frame arrived"
        assert received[0]["capture_backend"] == ""
