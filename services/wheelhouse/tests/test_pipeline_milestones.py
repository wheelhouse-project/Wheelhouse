"""Tests for pipeline milestone logging (Feature 2: Observability).

Verifies that INFO-level milestone log records are emitted at each
pipeline stage boundary using the ``wheelhouse.pipeline`` logger.

Milestones: STT_RECEIVED, ROUTED, EXECUTING, DICTATING, IPC_SENT, IPC_COMPLETE, INPUT_RECEIVED.

Tests use the SpeechPipelineHarness and caplog to capture log records,
then assert on milestone names, ordering, and elapsed_ms values.
"""

import asyncio
import logging
import re
import pytest

from utils.trace_context import TraceIdFilter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PIPELINE_LOGGER = "wheelhouse.pipeline"


def _pipeline_records(caplog_records, trace_id=None):
    """Extract pipeline milestone records, optionally filtered by trace_id."""
    records = [r for r in caplog_records if r.name == PIPELINE_LOGGER]
    if trace_id:
        records = [r for r in records if getattr(r, "trace_id", "") == trace_id]
    return records


def _milestone_names(records):
    """Extract milestone names from the start of each record message.

    Expected format: ``ROUTED action=BUFFER mode=COMMAND ...``
    The milestone name is the first whitespace-delimited token.
    """
    return [r.getMessage().split()[0] for r in records if r.getMessage()]


def _extract_elapsed(record):
    """Extract elapsed_ms value from a log record message.

    Searches for ``elapsed_ms=<number>`` in the message.
    Returns None if not found.
    """
    match = re.search(r"elapsed_ms=([\d.]+)", record.getMessage())
    return float(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def harness():
    from tests.test_speech_pipeline import SpeechPipelineHarness
    return SpeechPipelineHarness()


@pytest.fixture
async def running_harness(harness):
    await harness.start()
    yield harness
    await harness.stop()


# ---------------------------------------------------------------------------
# Tests: Command flow milestones
# ---------------------------------------------------------------------------

class TestCommandMilestones:
    """Command utterance should produce: ROUTED, EXECUTING, IPC_SENT sequence."""

    @pytest.mark.asyncio
    async def test_command_produces_routed_milestone(self, running_harness, caplog):
        """ROUTED milestone logged after router.decide() for a command word."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "delete",
                    start_of_utterance=True,
                    utterance_id=1,
                    trace_id="T-000001",
                )
                await running_harness.send_word(
                    "",
                    end_of_utterance=True,
                    utterance_id=1,
                    is_utterance_end_marker=True,
                    trace_id="T-000001",
                )
                # Wait for command timeout + processing
                await running_harness.wait_for_timeout(1200)

            records = _pipeline_records(caplog.records, trace_id="T-000001")
            milestones = _milestone_names(records)
            assert "ROUTED" in milestones, (
                f"Expected ROUTED milestone. Got milestones: {milestones}"
            )
        finally:
            caplog.handler.removeFilter(trace_filter)

    @pytest.mark.asyncio
    async def test_command_produces_executing_milestone(self, running_harness, caplog):
        """EXECUTING milestone logged when command is dispatched."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "delete",
                    start_of_utterance=True,
                    utterance_id=2,
                    trace_id="T-000002",
                )
                await running_harness.send_word(
                    "",
                    end_of_utterance=True,
                    utterance_id=2,
                    is_utterance_end_marker=True,
                    trace_id="T-000002",
                )
                await running_harness.wait_for_timeout(1200)

            records = _pipeline_records(caplog.records, trace_id="T-000002")
            milestones = _milestone_names(records)
            assert "EXECUTING" in milestones, (
                f"Expected EXECUTING milestone. Got milestones: {milestones}"
            )
        finally:
            caplog.handler.removeFilter(trace_filter)

    @pytest.mark.asyncio
    async def test_command_milestone_order(self, running_harness, caplog):
        """Milestones for a command should appear in order: ROUTED before EXECUTING."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "delete",
                    start_of_utterance=True,
                    utterance_id=3,
                    trace_id="T-000003",
                )
                await running_harness.send_word(
                    "",
                    end_of_utterance=True,
                    utterance_id=3,
                    is_utterance_end_marker=True,
                    trace_id="T-000003",
                )
                await running_harness.wait_for_timeout(1200)

            records = _pipeline_records(caplog.records, trace_id="T-000003")
            milestones = _milestone_names(records)

            # Filter to just the ordering-relevant milestones
            ordered = [m for m in milestones if m in ("ROUTED", "EXECUTING")]
            assert ordered == ["ROUTED", "EXECUTING"], (
                f"Expected [ROUTED, EXECUTING] order. Got: {ordered}"
            )
        finally:
            caplog.handler.removeFilter(trace_filter)


# ---------------------------------------------------------------------------
# Tests: Dictation flow milestones
# ---------------------------------------------------------------------------

class TestDictationMilestones:
    """Passthrough word should produce: ROUTED, DICTATING, IPC_SENT sequence."""

    @pytest.mark.asyncio
    async def test_dictation_produces_routed_milestone(self, running_harness, caplog):
        """ROUTED milestone logged for a passthrough word."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "hello",
                    start_of_utterance=True,
                    utterance_id=10,
                    trace_id="T-000010",
                )
                await asyncio.sleep(0.1)

            records = _pipeline_records(caplog.records, trace_id="T-000010")
            milestones = _milestone_names(records)
            assert "ROUTED" in milestones, (
                f"Expected ROUTED milestone for dictation. Got: {milestones}"
            )
        finally:
            caplog.handler.removeFilter(trace_filter)

    @pytest.mark.asyncio
    async def test_dictation_produces_dictating_milestone(self, running_harness, caplog):
        """DICTATING milestone logged when text sent to dictation."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "hello",
                    start_of_utterance=True,
                    utterance_id=11,
                    trace_id="T-000011",
                )
                await asyncio.sleep(0.1)

            records = _pipeline_records(caplog.records, trace_id="T-000011")
            milestones = _milestone_names(records)
            assert "DICTATING" in milestones, (
                f"Expected DICTATING milestone. Got: {milestones}"
            )
        finally:
            caplog.handler.removeFilter(trace_filter)

    @pytest.mark.asyncio
    async def test_dictation_milestone_order(self, running_harness, caplog):
        """Milestones for dictation should appear in order: ROUTED before DICTATING."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "hello",
                    start_of_utterance=True,
                    utterance_id=12,
                    trace_id="T-000012",
                )
                await asyncio.sleep(0.1)

            records = _pipeline_records(caplog.records, trace_id="T-000012")
            milestones = _milestone_names(records)
            ordered = [m for m in milestones if m in ("ROUTED", "DICTATING")]
            assert ordered == ["ROUTED", "DICTATING"], (
                f"Expected [ROUTED, DICTATING] order. Got: {ordered}"
            )
        finally:
            caplog.handler.removeFilter(trace_filter)


# ---------------------------------------------------------------------------
# Tests: IPC milestones (tested via app.py directly, not the harness MockApp)
# ---------------------------------------------------------------------------

class TestIpcMilestones:
    """IPC_SENT milestone logged when payload enqueued to input process."""

    @pytest.fixture
    def app(self):
        """Minimal WheelHouseApp with mocked IPC primitives."""
        from unittest.mock import MagicMock
        from app import WheelHouseApp

        app = WheelHouseApp.__new__(WheelHouseApp)
        app.shm = MagicMock()
        app.command_ready_event = MagicMock()
        app.ui_ready_event = MagicMock()
        app.response_queue = MagicMock()
        app.response_futures = {}
        app.response_timeout_s = 5.0
        app._outbound_q = asyncio.Queue()
        app.demuxer_task = None
        app._sender_task = None
        app._ws_manager = None
        return app

    @pytest.mark.asyncio
    async def test_send_command_produces_ipc_sent(self, app, caplog):
        """IPC_SENT logged when send_command enqueues a payload."""
        from utils.trace_context import set_trace
        set_trace("T-000020")

        with caplog.at_level(logging.INFO):
            await app.send_command({"action": "press", "params": {"key": "enter"}})

        records = _pipeline_records(caplog.records)
        milestones = _milestone_names(records)
        assert "IPC_SENT" in milestones, (
            f"Expected IPC_SENT milestone. Got: {milestones}"
        )

    @pytest.mark.asyncio
    async def test_send_request_produces_ipc_sent(self, app, caplog):
        """IPC_SENT logged when send_request enqueues a payload."""
        from utils.trace_context import set_trace
        import unittest.mock

        set_trace("T-000021")

        async def instant_timeout(coro, timeout):
            raise asyncio.TimeoutError()

        with caplog.at_level(logging.INFO):
            with unittest.mock.patch("asyncio.wait_for", side_effect=instant_timeout):
                with pytest.raises(asyncio.TimeoutError):
                    await app.send_request("intelligent_insert_text", params={"insertion_string": "hi"})

        records = _pipeline_records(caplog.records)
        milestones = _milestone_names(records)
        assert "IPC_SENT" in milestones, (
            f"Expected IPC_SENT milestone. Got: {milestones}"
        )


# ---------------------------------------------------------------------------
# Tests: Elapsed timing
# ---------------------------------------------------------------------------

class TestElapsedTiming:
    """elapsed_ms values should be non-negative and non-decreasing within a trace."""

    @pytest.mark.asyncio
    async def test_elapsed_ms_present_in_milestones(self, running_harness, caplog):
        """All pipeline milestone records should contain elapsed_ms."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "hello",
                    start_of_utterance=True,
                    utterance_id=30,
                    trace_id="T-000030",
                )
                await asyncio.sleep(0.1)

            records = _pipeline_records(caplog.records, trace_id="T-000030")
            assert len(records) > 0, "Expected pipeline records"
            for r in records:
                elapsed = _extract_elapsed(r)
                assert elapsed is not None, (
                    f"Missing elapsed_ms in milestone: {r.getMessage()}"
                )
        finally:
            caplog.handler.removeFilter(trace_filter)

    @pytest.mark.asyncio
    async def test_elapsed_ms_non_negative(self, running_harness, caplog):
        """elapsed_ms should never be negative."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "hello",
                    start_of_utterance=True,
                    utterance_id=31,
                    trace_id="T-000031",
                )
                await asyncio.sleep(0.1)

            records = _pipeline_records(caplog.records, trace_id="T-000031")
            for r in records:
                elapsed = _extract_elapsed(r)
                if elapsed is not None:
                    assert elapsed >= 0, (
                        f"Negative elapsed_ms={elapsed} in: {r.getMessage()}"
                    )
        finally:
            caplog.handler.removeFilter(trace_filter)

    @pytest.mark.asyncio
    async def test_elapsed_ms_non_decreasing(self, running_harness, caplog):
        """elapsed_ms values should be non-decreasing across milestones in a trace."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "hello",
                    start_of_utterance=True,
                    utterance_id=32,
                    trace_id="T-000032",
                )
                await asyncio.sleep(0.1)

            records = _pipeline_records(caplog.records, trace_id="T-000032")
            elapsed_values = [_extract_elapsed(r) for r in records]
            elapsed_values = [v for v in elapsed_values if v is not None]

            for i in range(1, len(elapsed_values)):
                assert elapsed_values[i] >= elapsed_values[i - 1], (
                    f"elapsed_ms decreased: {elapsed_values[i - 1]} -> {elapsed_values[i]}"
                )
        finally:
            caplog.handler.removeFilter(trace_filter)


# ---------------------------------------------------------------------------
# Tests: Milestone content details
# ---------------------------------------------------------------------------

class TestMilestoneContent:
    """Milestone messages contain expected key=value pairs."""

    @pytest.mark.asyncio
    async def test_routed_contains_action_and_mode(self, running_harness, caplog):
        """ROUTED milestone should contain action= and mode= fields."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "hello",
                    start_of_utterance=True,
                    utterance_id=40,
                    trace_id="T-000040",
                )
                await asyncio.sleep(0.1)

            records = _pipeline_records(caplog.records, trace_id="T-000040")
            routed = [r for r in records if r.getMessage().startswith("ROUTED")]
            assert len(routed) > 0, "Expected ROUTED record"

            msg = routed[0].getMessage()
            assert "action=" in msg, f"ROUTED missing action= field: {msg}"
            assert "mode=" in msg, f"ROUTED missing mode= field: {msg}"
        finally:
            caplog.handler.removeFilter(trace_filter)

    @pytest.mark.asyncio
    async def test_executing_contains_command_text(self, running_harness, caplog):
        """EXECUTING milestone should contain command= field."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "delete",
                    start_of_utterance=True,
                    utterance_id=41,
                    trace_id="T-000041",
                )
                await running_harness.send_word(
                    "",
                    end_of_utterance=True,
                    utterance_id=41,
                    is_utterance_end_marker=True,
                    trace_id="T-000041",
                )
                await running_harness.wait_for_timeout(1200)

            records = _pipeline_records(caplog.records, trace_id="T-000041")
            executing = [r for r in records if r.getMessage().startswith("EXECUTING")]
            assert len(executing) > 0, "Expected EXECUTING record"

            msg = executing[0].getMessage()
            assert "command=" in msg, f"EXECUTING missing command= field: {msg}"
        finally:
            caplog.handler.removeFilter(trace_filter)

    @pytest.mark.asyncio
    async def test_dictating_contains_text(self, running_harness, caplog):
        """DICTATING milestone should contain text= field."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "hello",
                    start_of_utterance=True,
                    utterance_id=42,
                    trace_id="T-000042",
                )
                await asyncio.sleep(0.1)

            records = _pipeline_records(caplog.records, trace_id="T-000042")
            dictating = [r for r in records if r.getMessage().startswith("DICTATING")]
            assert len(dictating) > 0, "Expected DICTATING record"

            msg = dictating[0].getMessage()
            assert "text=" in msg, f"DICTATING missing text= field: {msg}"
        finally:
            caplog.handler.removeFilter(trace_filter)

    @pytest.mark.asyncio
    async def test_ipc_sent_contains_action(self, caplog):
        """IPC_SENT milestone should contain action= field."""
        from unittest.mock import MagicMock
        from app import WheelHouseApp
        from utils.trace_context import set_trace

        app = WheelHouseApp.__new__(WheelHouseApp)
        app.shm = MagicMock()
        app.command_ready_event = MagicMock()
        app.ui_ready_event = MagicMock()
        app.response_queue = MagicMock()
        app.response_futures = {}
        app.response_timeout_s = 5.0
        app._outbound_q = asyncio.Queue()
        app.demuxer_task = None
        app._sender_task = None
        app._ws_manager = None

        set_trace("T-000043")

        with caplog.at_level(logging.INFO):
            await app.send_command({"action": "press", "params": {"key": "a"}})

        records = _pipeline_records(caplog.records)
        ipc = [r for r in records if r.getMessage().startswith("IPC_SENT")]
        assert len(ipc) > 0, "Expected IPC_SENT record"

        msg = ipc[0].getMessage()
        assert "action=" in msg, f"IPC_SENT missing action= field: {msg}"


# ---------------------------------------------------------------------------
# Tests: Pipeline logger level
# ---------------------------------------------------------------------------

class TestPipelineLoggerLevel:
    """Pipeline milestones should be logged at INFO level."""

    @pytest.mark.asyncio
    async def test_milestones_are_info_level(self, running_harness, caplog):
        """All pipeline milestones should be at INFO level."""
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await running_harness.send_word(
                    "hello",
                    start_of_utterance=True,
                    utterance_id=50,
                    trace_id="T-000050",
                )
                await asyncio.sleep(0.1)

            records = _pipeline_records(caplog.records, trace_id="T-000050")
            assert len(records) > 0, "Expected pipeline milestone records"
            for r in records:
                assert r.levelno == logging.INFO, (
                    f"Expected INFO level, got {r.levelname} for: {r.getMessage()}"
                )
        finally:
            caplog.handler.removeFilter(trace_filter)
