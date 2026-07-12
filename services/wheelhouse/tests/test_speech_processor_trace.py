"""Tests for trace_id ContextVar setting in SpeechProcessor.

ContextVars are copied on asyncio.Task creation, so set_trace() inside the
processor's Task isn't visible from the test coroutine.  We verify the effect
indirectly through log records (TraceIdFilter reads the ContextVar at emit
time inside the same Task).
"""

import asyncio
import logging
import pytest

from utils.trace_context import TraceIdFilter


class TestSpeechProcessorSetsTrace:
    """SpeechProcessor.process_word_event sets ContextVar from WordEvent.trace_id."""

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
    async def test_trace_id_in_log_records(self, running_harness, caplog):
        """Log records emitted during processing carry trace_id from ContextVar."""
        harness = running_harness

        # Add TraceIdFilter to caplog's handler so records get trace_id attribute
        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await harness.send_word(
                    "delete",
                    start_of_utterance=True,
                    utterance_id=2,
                    trace_id="T-000099",
                )
                await harness.send_word(
                    "",
                    end_of_utterance=True,
                    utterance_id=2,
                    is_utterance_end_marker=True,
                    trace_id="T-000099",
                )
                await asyncio.sleep(0.2)

            # Records from speech_processor should have trace_id set
            traced = [
                r for r in caplog.records
                if getattr(r, "trace_id", "") == "T-000099"
            ]
            assert len(traced) > 0, (
                "No log records found with trace_id=T-000099. "
                f"Records: {[(r.name, getattr(r, 'trace_id', '')) for r in caplog.records]}"
            )
        finally:
            caplog.handler.removeFilter(trace_filter)

    @pytest.mark.asyncio
    async def test_different_utterances_get_different_trace_ids(self, running_harness, caplog):
        """Two utterances with different trace_ids produce correctly tagged log records."""
        harness = running_harness

        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                # First utterance
                await harness.send_word(
                    "hello",
                    start_of_utterance=True,
                    utterance_id=10,
                    trace_id="T-000010",
                )
                await harness.send_word(
                    "",
                    end_of_utterance=True,
                    utterance_id=10,
                    is_utterance_end_marker=True,
                    trace_id="T-000010",
                )
                await asyncio.sleep(0.2)

                # Second utterance
                await harness.send_word(
                    "world",
                    start_of_utterance=True,
                    utterance_id=11,
                    trace_id="T-000011",
                )
                await harness.send_word(
                    "",
                    end_of_utterance=True,
                    utterance_id=11,
                    is_utterance_end_marker=True,
                    trace_id="T-000011",
                )
                await asyncio.sleep(0.2)

            t10 = [r for r in caplog.records if getattr(r, "trace_id", "") == "T-000010"]
            t11 = [r for r in caplog.records if getattr(r, "trace_id", "") == "T-000011"]
            assert len(t10) > 0, "No records with T-000010"
            assert len(t11) > 0, "No records with T-000011"
        finally:
            caplog.handler.removeFilter(trace_filter)

    @pytest.mark.asyncio
    async def test_no_trace_id_sets_empty_string(self, running_harness, caplog):
        """WordEvent without trace_id results in empty trace_id on log records."""
        harness = running_harness

        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.DEBUG):
                await harness.send_word(
                    "hello",
                    start_of_utterance=True,
                    utterance_id=20,
                    # No trace_id
                )
                await harness.send_word(
                    "",
                    end_of_utterance=True,
                    utterance_id=20,
                    is_utterance_end_marker=True,
                )
                await asyncio.sleep(0.1)

            processor_records = [
                r for r in caplog.records
                if r.name == "speech.speech_processor"
            ]
            assert len(processor_records) > 0
            # All should have empty trace_id (not some stale value)
            for r in processor_records:
                assert getattr(r, "trace_id", None) == "", (
                    f"Expected empty trace_id, got '{getattr(r, 'trace_id', 'N/A')}'"
                )
        finally:
            caplog.handler.removeFilter(trace_filter)
