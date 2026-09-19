"""Forwarded provider log records keep their source time.

wh-forwarded-log-time-order defect 1: the provider sends each forwarded
log record with an accurate source timestamp, but the receiver read it
into a variable and then stamped the record with its own arrival clock.
After a disconnect the provider's queue drains late on reconnect, so
wheelhouse.log showed every drained line at arrival time -- late by the
whole reconnect delay. The load-diagnostic reading guide
(docs/testing/stt-cpu-load-test-procedure.md) tells the operator to
count start_s_ago/end_s_ago back from the line's own timestamp, which
only works when that timestamp is the provider's source time.
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest


MANAGER_LOGGER = "integrations.websocket_manager"


class TestForwardedLogSourceTime:
    @staticmethod
    def _manager():
        from integrations.websocket_manager import WebSocketManager

        manager = WebSocketManager(loop=asyncio.get_running_loop())
        # These tests are about the forwarded-log path; the toast path
        # needs a state manager, so leave it out and let that path be
        # skipped.
        manager.state_manager = None
        return manager

    @staticmethod
    def _client(frames):
        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 9999)
        ws.send = AsyncMock()
        ws.__aiter__ = Mock(return_value=frames)
        return ws

    @staticmethod
    def _log_frame(**overrides):
        frame = {
            "type": "log",
            "level": "WARNING",
            "message": (
                "[load-diag] capture-outage start_s_ago=40 end_s_ago=10"
            ),
            "source": "Parakeet",
        }
        frame.update(overrides)
        return json.dumps(frame)

    @staticmethod
    def _forwarded_records(caplog):
        return [
            r
            for r in caplog.records
            if r.name == MANAGER_LOGGER
            and r.getMessage().startswith("[Parakeet]")
        ]

    async def _run_one_frame(self, caplog, frame):
        async def _frames():
            yield frame

        manager = self._manager()
        ws = self._client(_frames())
        with caplog.at_level(logging.DEBUG, logger=MANAGER_LOGGER):
            await manager.handle_connection(ws)

    @pytest.mark.asyncio
    async def test_a_delayed_record_keeps_its_source_time(self, caplog):
        """A record drained late carries the provider's source time.

        The frame's timestamp is 30 seconds before arrival, the same
        shape as a queue draining after a reconnect. The log record's
        created time must be the source time, not the arrival time.
        """
        source_epoch = time.time() - 30.0
        source_iso = datetime.fromtimestamp(source_epoch).isoformat()

        await self._run_one_frame(
            caplog, self._log_frame(timestamp=source_iso)
        )

        records = self._forwarded_records(caplog)
        assert len(records) == 1
        assert records[0].created == pytest.approx(source_epoch, abs=0.005)

    @pytest.mark.asyncio
    async def test_msecs_matches_the_source_time(self, caplog):
        """The formatter renders %(asctime)s from created AND msecs;
        a stale msecs would show the arrival milliseconds inside a
        source-time second. The offset is deliberately a non-integer
        number of seconds: an integer offset keeps the same fractional
        second, and this test could never fail."""
        source_epoch = time.time() - 30.456
        source_iso = datetime.fromtimestamp(source_epoch).isoformat()

        await self._run_one_frame(
            caplog, self._log_frame(timestamp=source_iso)
        )

        (record,) = self._forwarded_records(caplog)
        expected_msecs = (source_epoch - int(source_epoch)) * 1000
        assert record.msecs == pytest.approx(expected_msecs, abs=5.0)

    @pytest.mark.asyncio
    async def test_level_and_message_survive(self, caplog):
        """The source-time change must not alter level mapping or the
        [source] message prefix."""
        source_iso = datetime.now().isoformat()

        await self._run_one_frame(
            caplog, self._log_frame(timestamp=source_iso)
        )

        (record,) = self._forwarded_records(caplog)
        assert record.levelno == logging.WARNING
        assert record.getMessage() == (
            "[Parakeet] [load-diag] capture-outage "
            "start_s_ago=40 end_s_ago=10"
        )

    @pytest.mark.asyncio
    async def test_a_missing_timestamp_falls_back_to_arrival_time(
        self, caplog
    ):
        """A frame with no timestamp keeps today's behavior: the record
        is stamped on arrival and still logged."""
        before = time.time()
        frame = json.loads(self._log_frame())
        assert "timestamp" not in frame

        await self._run_one_frame(caplog, json.dumps(frame))

        (record,) = self._forwarded_records(caplog)
        assert before <= record.created <= time.time()

    @pytest.mark.asyncio
    async def test_a_malformed_timestamp_falls_back_to_arrival_time(
        self, caplog
    ):
        """A timestamp the receiver cannot parse must not drop the
        record; it is stamped on arrival."""
        before = time.time()

        await self._run_one_frame(
            caplog, self._log_frame(timestamp="not-a-time")
        )

        (record,) = self._forwarded_records(caplog)
        assert before <= record.created <= time.time()
