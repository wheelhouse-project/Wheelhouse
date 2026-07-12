"""Coverage gap tests for speech_processor.py.

Targets 8 specific uncovered code regions (26 lines) to bring
coverage from 84% toward 95%:

1. start() when already running (lines 225-226)
2. CancelledError re-raise in processing loop (line 274)
3. Error during processing resets to IDLE (lines 283-286)
4. New utterance while buffering auto-finalizes (lines 331-333)
5. Action.IGNORE resets state (lines 360-362)
6. Action.TRANSITION for hotword (lines 392-399)
7. Unmatched command falls through to dictation (lines 408-409)
8. Remainder infinite loop guard (lines 434-436)
"""
import sys
from pathlib import Path

# Path setup matching test_speech_pipeline.py
test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
patterns_path = wheelhouse_dir / "speech" / "config" / "patterns.toml"
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import asyncio
import pytest
from typing import List, Dict, Any, Optional
from unittest.mock import MagicMock

from speech.word_event import WordEvent
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from speech.command_engine import TextParser
from speech.domain import ProcessingMode, Action, Decision


# ============================================================================
# MOCK COMPONENTS
# ============================================================================

class MockApp:
    """Minimal mock app for recording send_command/send_request calls."""

    def __init__(self):
        self.actions: List[Dict[str, Any]] = []
        self.call_count = 0

    async def send_command(self, payload: dict):
        self.actions.append(payload)
        self.call_count += 1

    async def send_request(self, action: str, params: dict):
        payload = {"action": action, "params": params}
        self.actions.append(payload)
        self.call_count += 1
        return {"status": "success"}

    def clear(self):
        self.actions.clear()
        self.call_count = 0


class MockContextMirror:
    """Mock context mirror that doesn't use shared memory."""

    def __init__(self):
        self._context = {"app_name": "TestApp", "window_title": "Test Window", "timestamp": 0.0}

    def init_reader(self):
        pass

    def read_context(self) -> dict:
        return self._context


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def mock_app():
    return MockApp()


@pytest.fixture
def catalog():
    return PatternCatalog(str(patterns_path))


@pytest.fixture
def text_parser(mock_app, catalog):
    mock_speech_handler = MagicMock()
    mock_speech_handler.app = mock_app
    return TextParser(mock_speech_handler, catalog)


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


# ============================================================================
# 1. start() WHEN ALREADY RUNNING (lines 225-226)
# ============================================================================

@pytest.mark.asyncio
async def test_start_when_already_running_is_noop(processor):
    """Calling start() twice should log warning and not create a second task."""
    await processor.start()
    first_task = processor.processor_task

    # Second start should be a no-op
    await processor.start()
    assert processor.processor_task is first_task, "Second start() replaced the processing task"

    # Cleanup
    await processor.stop()


# ============================================================================
# 2. CancelledError RE-RAISE IN PROCESSING LOOP (line 274)
# ============================================================================

@pytest.mark.asyncio
async def test_cancelled_error_propagates_through_processing_loop(processor):
    """CancelledError during word processing must propagate, not be swallowed."""
    # Put a word in the queue
    await processor.word_queue.put(
        WordEvent("hello", start_of_utterance=True, end_of_utterance=False)
    )

    # Patch process_word_event to raise CancelledError
    original_process = processor.process_word_event
    call_count = 0

    async def cancel_on_process(word_event):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise asyncio.CancelledError()
        return await original_process(word_event)

    processor.process_word_event = cancel_on_process

    # Start the loop - it should process the word and get CancelledError
    await processor.start()

    # Wait for the task to finish (should be cancelled)
    with pytest.raises(asyncio.CancelledError):
        await processor.processor_task


# ============================================================================
# 3. ERROR DURING PROCESSING RESETS TO IDLE (lines 283-286)
# ============================================================================

@pytest.mark.asyncio
async def test_processing_error_resets_buffering_to_idle(processor, mock_app):
    """Exception during word processing while buffering resets to IDLE.

    The processing loop should:
    - Log the error
    - Reset mode to IDLE
    - Clear the buffer
    - Clear hotword_active
    - Continue processing the next word
    """
    # Put processor into COMMAND_BUFFERING via a fresh command word
    await processor.process_word_event(
        WordEvent("delete", start_of_utterance=True, end_of_utterance=False)
    )
    assert processor.mode == ProcessingMode.COMMAND_BUFFERING

    # Cancel any existing timeout from the first word
    processor._cancel_timeout()

    # Put a "bad" word that will raise, then a sentinel to verify recovery
    error_word = WordEvent("bad", start_of_utterance=False, end_of_utterance=False)
    good_word = WordEvent("hello", start_of_utterance=True, end_of_utterance=False)

    await processor.word_queue.put(error_word)
    await processor.word_queue.put(good_word)

    # Patch process_word_event to raise on "bad", pass through on others
    original_process = processor.process_word_event

    async def flaky_process(word_event):
        if word_event.word == "bad":
            raise RuntimeError("Simulated processing error")
        return await original_process(word_event)

    processor.process_word_event = flaky_process

    # Start the processing loop
    loop_task = asyncio.create_task(processor._processing_loop())

    # Give it time to process both words
    await asyncio.sleep(0.1)

    # After error recovery + processing "hello", should be in IDLE
    assert processor.mode == ProcessingMode.IDLE
    assert len(processor.buffer) == 0
    assert processor.hotword_active is False

    # The good word should have been processed (dictation sent)
    assert mock_app.call_count > 0

    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass


# ============================================================================
# 4. NEW UTTERANCE WHILE BUFFERING AUTO-FINALIZES (lines 331-333)
# ============================================================================

@pytest.mark.asyncio
async def test_new_utterance_while_buffering_finalizes_previous(processor, mock_app):
    """Starting a new utterance while buffering auto-finalizes the previous buffer."""
    # Enter COMMAND_BUFFERING with "delete"
    await processor.process_word_event(
        WordEvent("delete", start_of_utterance=True, end_of_utterance=False)
    )
    assert processor.mode == ProcessingMode.COMMAND_BUFFERING
    assert "delete" in processor.buffer

    # New utterance starts before timeout - should finalize "delete" buffer first
    await processor.process_word_event(
        WordEvent("hello", start_of_utterance=True, end_of_utterance=False)
    )

    # After auto-finalization + processing "hello", should be back in IDLE
    assert processor.mode == ProcessingMode.IDLE
    assert len(processor.buffer) == 0

    # Both "delete" finalization and "hello" dictation should have produced actions
    assert mock_app.call_count >= 2


# ============================================================================
# 5. Action.IGNORE RESETS STATE (lines 360-362)
# ============================================================================

@pytest.mark.asyncio
async def test_ignore_action_resets_to_idle_and_sends_pending_end(processor, mock_app):
    """Action.IGNORE should reset to IDLE and send any pending utterance end."""
    # Set up pending utterance end
    processor._pending_utterance_end = 42

    # Directly call _execute_decision with IGNORE
    decision = Decision(action=Action.IGNORE, reason="test ignore")
    await processor._execute_decision(decision)

    # Should reset to IDLE
    assert processor.mode == ProcessingMode.IDLE
    assert len(processor.buffer) == 0
    assert processor.hotword_active is False

    # Should have sent the pending end_utterance
    end_actions = [a for a in mock_app.actions if a.get("action") == "end_utterance"]
    assert len(end_actions) == 1
    assert end_actions[0]["params"]["utterance_id"] == 42
    assert processor._pending_utterance_end is None


@pytest.mark.asyncio
async def test_ignore_action_no_pending_end(processor, mock_app):
    """Action.IGNORE with no pending utterance end should just reset."""
    decision = Decision(action=Action.IGNORE, reason="test ignore")
    await processor._execute_decision(decision)

    assert processor.mode == ProcessingMode.IDLE
    assert mock_app.call_count == 0


# ============================================================================
# 6. Action.TRANSITION FOR HOTWORD (lines 392-399)
# ============================================================================

@pytest.mark.asyncio
async def test_hotword_transitions_to_hotword_buffering(processor, mock_app):
    """Hotword at utterance start should transition to HOTWORD_BUFFERING."""
    await processor.process_word_event(
        WordEvent("x-ray", start_of_utterance=True, end_of_utterance=False)
    )

    assert processor.mode == ProcessingMode.HOTWORD_BUFFERING
    assert processor.hotword_active is True
    # Hotword itself should NOT be in the buffer
    assert "x-ray" not in processor.buffer


@pytest.mark.asyncio
async def test_transition_sets_timeout(processor, mock_app):
    """TRANSITION action should start a timeout when specified."""
    decision = Decision(
        action=Action.TRANSITION,
        target_mode=ProcessingMode.HOTWORD_BUFFERING,
        timeout_ms=1000,
        reason="test transition",
    )
    await processor._execute_decision(decision)

    assert processor.mode == ProcessingMode.HOTWORD_BUFFERING
    assert processor.hotword_active is True
    assert processor.timeout_task is not None
    assert not processor.timeout_task.done()

    # Cleanup
    processor._cancel_timeout()


@pytest.mark.asyncio
async def test_transition_without_hotword_mode(processor, mock_app):
    """TRANSITION to a non-hotword mode should not set hotword_active."""
    decision = Decision(
        action=Action.TRANSITION,
        target_mode=ProcessingMode.COMMAND_BUFFERING,
        timeout_ms=500,
        reason="test non-hotword transition",
    )
    await processor._execute_decision(decision)

    assert processor.mode == ProcessingMode.COMMAND_BUFFERING
    assert processor.hotword_active is False

    # Cleanup
    processor._cancel_timeout()


# ============================================================================
# 7. UNMATCHED COMMAND FALLS THROUGH TO DICTATION (lines 408-409)
# ============================================================================

@pytest.mark.asyncio
async def test_unmatched_command_sends_to_dictation(processor, mock_app):
    """When _execute_command gets text that matches no pattern, it dictates."""
    await processor._execute_command("xyzzy zorp blam")

    insert_actions = [
        a for a in mock_app.actions
        if a.get("action") == "intelligent_insert_text"
    ]
    assert len(insert_actions) == 1
    assert insert_actions[0]["params"]["insertion_string"] == "xyzzy zorp blam"


# ============================================================================
# 8. REMAINDER EMPTY-SPAN GUARD (wh-oe7u.1 / wh-oe7u.2)
# ============================================================================

@pytest.mark.asyncio
async def test_remainder_empty_span_guard_dictates_and_returns(processor, mock_app):
    """If the helper returns a match with match.end() == match.start(),
    advancing on the after-text alone would loop forever. The new
    _process_remainder dictates the remainder verbatim and returns
    (wh-oe7u.1 / wh-oe7u.2)."""
    import re

    fake_match = re.search("()", "stuck text")  # zero-width match at start
    fake_pattern_data = {"actions": [], "validation_group": None}

    def fake_finder(text):
        return fake_match, fake_pattern_data

    processor._find_earliest_replacement = fake_finder

    await processor._process_remainder("stuck text")

    insert_actions = [
        a for a in mock_app.actions
        if a.get("action") == "intelligent_insert_text"
    ]
    assert len(insert_actions) == 1
    assert insert_actions[0]["params"]["insertion_string"] == "stuck text"


# ============================================================================
# INTEGRATION: HOTWORD FULL FLOW
# ============================================================================

@pytest.mark.asyncio
async def test_hotword_followed_by_command_executes(processor, mock_app):
    """Hotword followed by a requires_hotword command should execute."""
    await processor.process_word_event(
        WordEvent("x-ray", start_of_utterance=True, end_of_utterance=False)
    )
    assert processor.mode == ProcessingMode.HOTWORD_BUFFERING
    assert processor.hotword_active is True

    await processor.process_word_event(
        WordEvent("close", start_of_utterance=False, end_of_utterance=False)
    )
    assert len(processor.buffer) >= 1

    await processor.process_word_event(
        WordEvent("window", start_of_utterance=False, end_of_utterance=False)
    )

    # Wait for timeout if needed
    await asyncio.sleep(1.1)

    assert processor.mode == ProcessingMode.IDLE
    assert mock_app.call_count > 0


@pytest.mark.asyncio
async def test_hotword_alone_times_out_to_ignore(processor, mock_app):
    """Hotword alone (no following command) should timeout and be ignored.

    wh-oe7u.4: timeout finalization runs through the word_queue, so the
    processing loop must be live for the sentinel to be consumed and the
    state machine to return to IDLE.
    """
    await processor.start()
    try:
        await processor.process_word_event(
            WordEvent("x-ray", start_of_utterance=True, end_of_utterance=False)
        )
        assert processor.mode == ProcessingMode.HOTWORD_BUFFERING

        # Wait for timeout to fire and for the sentinel to be processed.
        await asyncio.sleep(1.1)

        assert processor.mode == ProcessingMode.IDLE
        assert processor.hotword_active is False
    finally:
        await processor.stop()


# ============================================================================
# wh-qj70s: hotword-required commands in replacement remainder must NOT execute
# ============================================================================


@pytest.mark.asyncio
async def test_hotword_required_command_in_remainder_is_dictated(processor, mock_app):
    """A hotword-required command that lands in a replacement remainder must
    be dictated, not executed (wh-qj70s).

    Pre-fix: ``_process_remainder`` called ``TextParser.parse_and_execute``,
    which used ``PatternMatcher.match_single_pattern`` without any hotword
    gate. ``^save$`` (``requires_hotword = true``) would match and fire its
    ``hotkey_action`` (ctrl+s) even though the user never said the hotword.

    Post-fix: the remainder path passes ``authorized_command=False`` (the
    default), and the matcher refuses any pattern with ``requires_hotword``.
    """
    await processor._process_remainder("save")

    hotkey_actions = [
        a for a in mock_app.actions if a.get("action") == "hotkey_action"
    ]
    assert hotkey_actions == [], (
        f"Hotword-required command 'save' fired hotkey action(s) via "
        f"remainder path with no hotword: {hotkey_actions}"
    )
    insert_actions = [
        a for a in mock_app.actions
        if a.get("action") == "intelligent_insert_text"
    ]
    assert len(insert_actions) == 1
    assert insert_actions[0]["params"]["insertion_string"] == "save"


@pytest.mark.asyncio
async def test_authorized_command_path_still_executes_hotword_command(processor, mock_app):
    """The router-vetted command path (``_execute_command``) sets
    ``authorized_command=True`` and must still execute hotword-required
    patterns. This is the regression guard for wh-qj70s -- the gate must
    refuse only the unauthorized remainder path, not the legitimate one.
    """
    await processor._execute_command("save")

    hotkey_actions = [
        a for a in mock_app.actions if a.get("action") == "hotkey_action"
    ]
    assert len(hotkey_actions) >= 1, (
        "Authorized 'save' command did not fire its hotkey action; the "
        "wh-qj70s fix broke the legitimate router-vetted path."
    )
    assert hotkey_actions[0]["params"]["keys"] == ["ctrl", "s"]


# ============================================================================
# wh-3pvsu: stop() must cancel pending timeout and clear pending utterance end
# ============================================================================


@pytest.mark.asyncio
async def test_stop_cancels_pending_timeout_task(processor):
    """stop() must cancel any pending _timeout_handler task (wh-3pvsu).

    Pre-fix: stop() only cancelled processor_task. A timeout already armed by
    a buffering decision would survive shutdown, fire after stop returned,
    and run dictation or a command on the way out.
    """
    await processor.start()
    processor._start_timeout(5000)  # 5s, far longer than the test
    timeout_task = processor.timeout_task
    assert timeout_task is not None
    assert not timeout_task.done()

    await processor.stop()

    assert processor.timeout_task is None, (
        "stop() left timeout_task reference behind"
    )
    assert timeout_task.done() or timeout_task.cancelled(), (
        "stop() did not cancel the pending timeout task; it can fire after "
        "shutdown and run dictation or a command (wh-3pvsu)."
    )


@pytest.mark.asyncio
async def test_stop_clears_pending_utterance_end(processor):
    """stop() clears _pending_utterance_end so a deferred end never escapes
    across shutdown (wh-3pvsu)."""
    await processor.start()
    processor._pending_utterance_end = 99

    await processor.stop()

    assert processor._pending_utterance_end is None, (
        "stop() must clear _pending_utterance_end so a stale deferred end "
        "cannot fire after shutdown."
    )


@pytest.mark.asyncio
async def test_timeout_after_stop_is_noop(processor, mock_app):
    """If a timeout task somehow wakes after stop(), it must not enqueue
    a sentinel or run a decision (wh-3pvsu / wh-oe7u.4).

    Belt-and-suspenders guard on top of the cancel: even if cancellation
    races with the asyncio.sleep wake, the handler must check
    ``self._stopped`` and return without enqueuing the sentinel. The
    consumer-side token check is the second line of defense.
    """
    await processor.start()
    processor.mode = ProcessingMode.COMMAND_BUFFERING
    processor.buffer.append("nonsense")
    processor._start_timeout(50_000)
    snapshot_token = processor.timeout_token
    assert processor.timeout_task is not None

    await processor.stop()
    mock_app.clear()

    # Invoke the handler-equivalent path directly to simulate a wake-after-stop
    # race. The stop guard should skip the put_nowait. wh-oe7u.4: pass the
    # snapshot token the cancelled task would have carried.
    await processor._timeout_handler(0, snapshot_token)

    # The sentinel must NOT be on the queue (the stopped guard skipped
    # the enqueue), and no _execute_decision side effects should appear.
    assert mock_app.call_count == 0, (
        f"Timeout handler ran a decision after stop(): {mock_app.actions}"
    )
    assert processor.word_queue.empty(), (
        "Timeout handler enqueued a sentinel after stop(); the stopped "
        "guard must skip put_nowait so the queue stays clean across "
        "shutdown."
    )


# ============================================================================
# wh-bvl6d / wh-oe7u.4: insert before end_utterance ordering
# ============================================================================
#
# wh-bvl6d originally added the _inflight_finalization flag so that an
# utterance_end_marker dequeued during a timeout-driven insert IPC await
# would defer instead of firing end_utterance against an already-IDLE
# mode (clipboard restore would otherwise race the paste).
#
# wh-oe7u.4 deletes the flag because it became unnecessary. Timeout
# finalization now runs through the same word_queue as every other
# event via a typed ``timeout-finalize`` sentinel, so the processing
# loop is the single writer. While _execute_decision awaits the insert
# IPC the loop cannot dequeue more events, so a queued
# utterance_end_marker waits in the queue and is processed AFTER the
# IPC completes. The insert-before-end_utterance ordering still holds;
# the mechanism is now queue serialization rather than a flag-based
# defer.


@pytest.mark.asyncio
async def test_pending_utterance_end_flushes_after_execute_decision(
    processor, mock_app,
):
    """The deferred end_utterance must fire via _send_pending_utterance_end
    after _execute_decision returns, regardless of the dictation IPC
    timing (wh-bvl6d, post-wh-oe7u.4)."""
    processor.mode = ProcessingMode.IDLE
    processor._pending_utterance_end = 42

    await processor._send_pending_utterance_end()

    end_actions = [a for a in mock_app.actions if a.get("action") == "end_utterance"]
    assert len(end_actions) == 1
    assert end_actions[0]["params"]["utterance_id"] == 42
    assert processor._pending_utterance_end is None


@pytest.mark.asyncio
async def test_timeout_dictation_orders_insert_before_end_utterance(
    processor, mock_app,
):
    """Race-shape integration test (wh-bvl6d / wh-oe7u.4): a timeout-driven
    DICTATE happens while an utterance_end_marker is enqueued. The
    send_request to insert text must complete BEFORE end_utterance is
    sent.

    Mechanism: timeout finalization enqueues a ``timeout-finalize``
    sentinel onto the same word_queue as the marker. The processing
    loop dequeues the sentinel first (it was enqueued first), runs
    _execute_decision (which awaits the slow insert), then dequeues
    the marker. Order preserved.
    """
    # Make send_request slow so the ordering check is observable.
    insert_started = asyncio.Event()
    insert_release = asyncio.Event()
    original_send_request = processor.app.send_request

    async def slow_send_request(action, params):
        if action == "intelligent_insert_text":
            insert_started.set()
            await insert_release.wait()
        return await original_send_request(action, params)

    processor.app.send_request = slow_send_request

    await processor.start()
    try:
        # Buffer a non-command word so decide_timeout returns DICTATE.
        processor.mode = ProcessingMode.COMMAND_BUFFERING
        processor.buffer.append("xyzzy")
        # Bump the token and enqueue the sentinel as if the timer fired.
        processor.timeout_token += 1
        sentinel_token = processor.timeout_token
        processor.word_queue.put_nowait(
            WordEvent.timeout_finalize(token=sentinel_token)
        )

        # Now enqueue the utterance_end_marker. It must wait in the
        # queue until the sentinel-driven _execute_decision completes
        # the slow IPC.
        await insert_started.wait()
        marker = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=True,
            utterance_id=77,
            is_utterance_end_marker=True,
        )
        processor.word_queue.put_nowait(marker)

        # End_utterance must NOT have fired yet -- the loop is blocked
        # on the slow insert.
        end_actions_during = [
            a for a in mock_app.actions if a.get("action") == "end_utterance"
        ]
        assert end_actions_during == [], (
            f"end_utterance fired before insert IPC completed: "
            f"{mock_app.actions}"
        )

        # Release the IPC and let the processing loop drain.
        insert_release.set()
        # Give the loop time to process both events.
        for _ in range(50):
            await asyncio.sleep(0.01)
            end_actions_now = [
                a for a in mock_app.actions
                if a.get("action") == "end_utterance"
            ]
            if end_actions_now:
                break

        # Final action order: intelligent_insert_text BEFORE end_utterance.
        actions_of_interest = [
            a["action"] for a in mock_app.actions
            if a.get("action") in ("intelligent_insert_text", "end_utterance")
        ]
        assert actions_of_interest == ["intelligent_insert_text", "end_utterance"], (
            f"Wrong action order; expected insert then end_utterance, "
            f"got {actions_of_interest}. Full actions: {mock_app.actions}"
        )
    finally:
        await processor.stop()
