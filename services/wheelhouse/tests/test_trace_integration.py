"""Integration test: trace_id threading through the full speech pipeline.

Verifies that trace_id flows from WordEvent through SpeechProcessor into
MockApp's captured outputs, proving the ContextVar is set correctly for
downstream consumers (app.py's get_trace_id() call).
"""

import asyncio
import logging
import pytest

from utils.trace_context import TraceIdFilter


class TestTraceIdEndToEnd:
    """End-to-end trace_id threading through the pipeline."""

    @pytest.fixture
    def harness(self):
        from tests.test_speech_pipeline import SpeechPipelineHarness

        return SpeechPipelineHarness()

    @pytest.fixture
    async def running_harness(self, harness):
        await harness.start()
        yield harness
        await harness.stop()

    @pytest.mark.asyncio
    async def test_dictation_carries_trace_id(self, running_harness):
        """Passthrough dictation word -> MockApp output has correct trace_id."""
        harness = running_harness
        await harness.send_word(
            "hello",
            start_of_utterance=True,
            utterance_id=100,
            trace_id="T-000100",
        )
        await harness.send_word(
            "",
            end_of_utterance=True,
            utterance_id=100,
            is_utterance_end_marker=True,
            trace_id="T-000100",
        )
        await asyncio.sleep(0.15)

        # Find the dictation output (intelligent_insert_text or similar)
        traced_outputs = [o for o in harness.mock_app.outputs if o.trace_id == "T-000100"]
        assert len(traced_outputs) > 0, (
            f"No outputs with trace_id=T-000100. "
            f"Outputs: {[(o.action, o.trace_id) for o in harness.mock_app.outputs]}"
        )

    @pytest.mark.asyncio
    async def test_command_carries_trace_id(self, running_harness):
        """Command word -> execution -> MockApp output has correct trace_id."""
        harness = running_harness
        await harness.send_word(
            "enter",
            start_of_utterance=True,
            utterance_id=200,
            trace_id="T-000200",
        )
        await harness.send_word(
            "",
            end_of_utterance=True,
            utterance_id=200,
            is_utterance_end_marker=True,
            trace_id="T-000200",
        )
        # Wait for buffer timeout + processing
        await asyncio.sleep(1.5)

        traced_outputs = [o for o in harness.mock_app.outputs if o.trace_id == "T-000200"]
        assert len(traced_outputs) > 0, (
            f"No outputs with trace_id=T-000200. "
            f"Outputs: {[(o.action, o.trace_id) for o in harness.mock_app.outputs]}"
        )

    @pytest.mark.asyncio
    async def test_two_utterances_isolated(self, running_harness):
        """Two sequential utterances have distinct trace_ids on their outputs."""
        harness = running_harness

        # Utterance 1
        await harness.send_word("hello", start_of_utterance=True, utterance_id=301, trace_id="T-000301")
        await harness.send_word("", end_of_utterance=True, utterance_id=301,
                                is_utterance_end_marker=True, trace_id="T-000301")
        await asyncio.sleep(0.15)

        # Utterance 2
        await harness.send_word("world", start_of_utterance=True, utterance_id=302, trace_id="T-000302")
        await harness.send_word("", end_of_utterance=True, utterance_id=302,
                                is_utterance_end_marker=True, trace_id="T-000302")
        await asyncio.sleep(0.15)

        t301 = [o for o in harness.mock_app.outputs if o.trace_id == "T-000301"]
        t302 = [o for o in harness.mock_app.outputs if o.trace_id == "T-000302"]
        assert len(t301) > 0, "No outputs with T-000301"
        assert len(t302) > 0, "No outputs with T-000302"

    @pytest.mark.asyncio
    async def test_log_records_carry_trace_id(self, running_harness, caplog):
        """Log records from pipeline carry the correct trace_id throughout processing."""
        harness = running_harness

        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await harness.send_word("hello", start_of_utterance=True,
                                        utterance_id=400, trace_id="T-000400")
                await harness.send_word("", end_of_utterance=True, utterance_id=400,
                                        is_utterance_end_marker=True, trace_id="T-000400")
                await asyncio.sleep(0.15)

            traced_logs = [r for r in caplog.records if getattr(r, "trace_id", "") == "T-000400"]
            assert len(traced_logs) > 0, (
                "No log records with trace_id=T-000400 found across pipeline"
            )
        finally:
            caplog.handler.removeFilter(trace_filter)
