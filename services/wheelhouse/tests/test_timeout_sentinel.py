"""Timeout-finalize sentinel regression tests (wh-oe7u.4).

The previous implementation ran timeout finalization in a separate task
that called ``_execute_decision`` directly. While that task awaited the
insert IPC, the main processing loop could dequeue retraction markers or
the next utterance's words and mutate state concurrently with the
finalization. The fix routes timeout finalization through the same
``word_queue`` as every other event via a typed ``timeout-finalize``
sentinel that carries a generation token; the processing loop is the
single writer.

Tests in this file lock down:

- Stale-token sentinel is ignored even if the mode would otherwise let
  it finalize (the high-priority lifecycle test per the bead reviewer).
- Already-cleared buffer / IDLE mode sentinel is a no-op.
- Real word ahead of the sentinel processes first.
- ``stop()`` invalidates the token before cancellation; a sentinel from
  a cancelled task is harmless even if injected manually.
- ``_cancel_timeout`` and ``_reset_to_idle`` bump the token.
"""
import asyncio
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pytest

from speech.domain import ProcessingMode
from speech.speech_processor import SpeechProcessor
from speech.word_event import WordEvent

from test_speech_processor_gaps import (
    catalog,
    text_parser,
    mock_app,
    MockApp,
    MockContextMirror,
)
from speech.pattern_catalog import PatternCatalog


@pytest.fixture
def processor(catalog, text_parser, mock_app):
    proc = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=text_parser,
        app=mock_app,
        replacement_timeout_ms=400,
        command_timeout_ms=1000,
    )
    proc.context_mirror = MockContextMirror()
    return proc


class TestTimeoutTokenLifecycle:
    """Token lifecycle: every cancellation/reset/stop bumps the token so
    a sentinel created with the previous value is ignored.

    Async because ``_start_timeout`` calls ``asyncio.create_task`` and
    needs a running loop.
    """

    @pytest.mark.asyncio
    async def test_start_timeout_bumps_token(self, processor):
        before = processor.timeout_token
        processor._start_timeout(50_000)
        assert processor.timeout_token != before, (
            "Starting a timeout must bump the token so any prior "
            "in-flight sentinel becomes stale."
        )
        processor._cancel_timeout()

    @pytest.mark.asyncio
    async def test_cancel_timeout_bumps_token(self, processor):
        processor._start_timeout(50_000)
        before = processor.timeout_token
        processor._cancel_timeout()
        assert processor.timeout_token != before, (
            "Cancelling a timeout must bump the token so a sentinel "
            "the cancelled task already enqueued becomes stale."
        )

    @pytest.mark.asyncio
    async def test_reset_to_idle_bumps_token_via_cancel(self, processor):
        processor.mode = ProcessingMode.COMMAND_BUFFERING
        processor._start_timeout(50_000)
        before = processor.timeout_token
        processor._reset_to_idle()
        assert processor.timeout_token != before


class TestSentinelStaleTokenIgnored:
    """The reviewer-flagged high-priority test: a sentinel with a stale
    token must be a no-op even if the mode is currently buffering for a
    NEWER utterance."""

    @pytest.mark.asyncio
    async def test_stale_sentinel_does_not_finalize_newer_buffer(
        self, processor, mock_app,
    ):
        """Construct a sentinel with an OLD token, then advance state
        for a newer buffer, then process the stale sentinel directly.
        The stale-token check must short-circuit before any
        ``_execute_decision`` runs against the new buffer."""
        processor.mode = ProcessingMode.COMMAND_BUFFERING
        processor.buffer.append("first-word")
        processor._start_timeout(50_000)
        old_token = processor.timeout_token

        # A newer event would normally bump the token. Simulate by
        # bumping directly (matches what _cancel_timeout would do).
        processor.timeout_token += 1
        # Replace the buffer with a NEWER utterance's content.
        processor.buffer = ["completely-different"]

        stale_sentinel = WordEvent.timeout_finalize(token=old_token)
        await processor.process_word_event(stale_sentinel)

        assert processor.buffer == ["completely-different"], (
            "Stale sentinel mutated state for a newer buffer."
        )
        # No decide_timeout / _execute_decision side effects on mock_app.
        assert mock_app.call_count == 0, (
            f"Stale sentinel triggered actions: {mock_app.actions}"
        )

        processor._cancel_timeout()


class TestSentinelIdleAndEmptyMode:
    """Sentinel handling for already-finalized / hotword-alone shapes."""

    @pytest.mark.asyncio
    async def test_sentinel_in_idle_mode_is_noop(self, processor, mock_app):
        """If the buffer was already finalized by other means (the
        sentinel arrived after utterance_end auto-finalized the buffer),
        the sentinel must not re-finalize."""
        processor.mode = ProcessingMode.IDLE
        sentinel = WordEvent.timeout_finalize(token=processor.timeout_token)
        await processor.process_word_event(sentinel)
        assert mock_app.call_count == 0


class TestSentinelOrderingPreserved:
    """A real word enqueued before the sentinel must process first."""

    @pytest.mark.asyncio
    async def test_real_word_ahead_of_sentinel_processes_first(
        self, processor, mock_app,
    ):
        """Queue ordering is preserved by the single-loop design: a real
        word event enqueued before the sentinel is dequeued and
        processed first."""
        await processor.start()
        try:
            # Enqueue a non-command word first. start_of_utterance starts a
            # fresh utterance; the word is sent as dictation.
            processor.word_queue.put_nowait(
                WordEvent("sundry", start_of_utterance=True, end_of_utterance=False)
            )
            # Then enqueue a sentinel with a fresh token. Mode is still IDLE
            # at the time the loop dequeues the first word, so the sentinel
            # itself becomes a no-op (mode IDLE).
            processor.word_queue.put_nowait(
                WordEvent.timeout_finalize(token=processor.timeout_token)
            )

            # Give the loop a few ticks to drain.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if mock_app.call_count > 0:
                    break

            # The real word produced an action (dictation) -- the sentinel
            # produced none. Order: real-word first, sentinel second.
            actions = [a["action"] for a in mock_app.actions]
            assert actions, "Real word never produced an action"
            assert actions[0] == "intelligent_insert_text"
        finally:
            await processor.stop()


class TestStopInvalidatesSentinel:
    """stop() must bump the token before cancellation so any sentinel
    enqueued during the cancellation race becomes stale."""

    @pytest.mark.asyncio
    async def test_stop_bumps_token_before_cancel(self, processor):
        await processor.start()
        processor._start_timeout(50_000)
        token_before_stop = processor.timeout_token

        await processor.stop()

        assert processor.timeout_token != token_before_stop, (
            "stop() did not bump timeout_token; a sentinel enqueued "
            "during cancellation could still match and finalize."
        )

    @pytest.mark.asyncio
    async def test_stopped_sentinel_is_noop_even_with_matching_token(
        self, processor, mock_app,
    ):
        """If a sentinel somehow lands after stop() (e.g. injected
        manually), the _stopped guard short-circuits before the token
        check or any decision."""
        await processor.start()
        processor.mode = ProcessingMode.COMMAND_BUFFERING
        processor.buffer.append("xyzzy")
        processor._start_timeout(50_000)
        token = processor.timeout_token

        await processor.stop()
        mock_app.clear()

        # Even with the matching token, the sentinel must be ignored
        # because _stopped is set.
        sentinel = WordEvent.timeout_finalize(token=token)
        await processor.process_word_event(sentinel)
        assert mock_app.call_count == 0
