"""Processor-level regression for the GREEDY_TIMEOUT_MS wiring (wh-greedy-buffer-race).

The unit-level tests in tests/test_router_gaps.py call
SpeechRouter._decide_buffering directly with an explicit greedy_timeout_ms.
That proves the router returns the supplied value, but does not catch a
regression that drops the configured value somewhere between the
SpeechProcessor constructor and the timer call. The path covered here is:

    constructor stores self.greedy_timeout_ms
        -> threaded into self.router.decide(...)
        -> Decision.timeout_ms returned
        -> _execute_decision calls self._start_timeout(decision.timeout_ms)

A regression that hardcoded 700 in any of those layers would silently revert
the fix. The test asserts _start_timeout receives the configured non-default
value (1234) on the second word of "hey Google".
"""
import sys
from pathlib import Path

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
patterns_path = wheelhouse_dir / "speech" / "config" / "patterns.toml"
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import asyncio
import pytest
from unittest.mock import MagicMock

from speech.word_event import WordEvent
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from speech.command_engine import TextParser
from speech.domain import ProcessingMode


class _MockApp:
    def __init__(self):
        self.actions = []

    async def send_command(self, payload):
        self.actions.append(payload)

    async def send_request(self, action, params):
        self.actions.append({"action": action, "params": params})
        return {"status": "success"}


class _MockContextMirror:
    def init_reader(self):
        pass

    def read_context(self):
        return {"app_name": "TestApp", "window_title": "Test Window", "timestamp": 0.0}


@pytest.fixture
def catalog():
    return PatternCatalog(str(patterns_path))


@pytest.fixture
def app():
    return _MockApp()


@pytest.fixture
def text_parser(app, catalog):
    handler = MagicMock()
    handler.app = app
    return TextParser(handler, catalog)


@pytest.mark.asyncio
async def test_greedy_timer_uses_configured_value_through_processor(catalog, text_parser, app):
    """End-to-end: configured greedy_timeout_ms threads through to _start_timeout.

    Driving "hey" then "Google" must call _start_timeout with the configured
    1234 ms on the second word. The first word "hey" enters COMMAND_BUFFERING
    with the standard command timer; the second word makes the buffer match
    the greedy pattern ^hey Google.*$ and must use the long greedy timer.
    """
    processor = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=text_parser,
        app=app,
        replacement_timeout_ms=400,
        command_timeout_ms=700,
        greedy_timeout_ms=1234,
    )
    processor.context_mirror = _MockContextMirror()

    timer_calls: list[int] = []

    def spy_start_timeout(duration_ms):
        timer_calls.append(duration_ms)
        # Skip the real timer task; we only care about the duration argument.

    processor._start_timeout = spy_start_timeout

    await processor.process_word_event(
        WordEvent("hey", start_of_utterance=True, end_of_utterance=False)
    )
    assert processor.mode == ProcessingMode.COMMAND_BUFFERING
    assert processor.buffer == ["hey"]

    await processor.process_word_event(
        WordEvent("Google", start_of_utterance=False, end_of_utterance=False)
    )
    assert processor.mode == ProcessingMode.COMMAND_BUFFERING
    assert processor.buffer == ["hey", "Google"]

    # The second timer call corresponds to the greedy buffer continuation.
    # Whatever order or count of calls happens for "hey", the call triggered
    # by "Google" landing on a greedy-matched buffer must use 1234 ms.
    assert 1234 in timer_calls, (
        f"Expected _start_timeout to be called with the configured greedy "
        f"value 1234, but saw {timer_calls}. Regression in the wiring "
        f"between SpeechProcessor.__init__ and _start_timeout."
    )
    # The standard command timer must NOT bleed through to the greedy case.
    # Last call wins -- the most recent _start_timeout is the active deadline.
    assert timer_calls[-1] == 1234, (
        f"The deadline active after the greedy-matching word must be 1234, "
        f"but the most recent _start_timeout call was {timer_calls[-1]}."
    )


@pytest.mark.asyncio
async def test_non_greedy_buffer_keeps_standard_timer_through_processor(catalog, text_parser, app):
    """Sanity counter-test: a non-greedy buffering pattern must not use the greedy timer.

    Without this, a regression that always used greedy_timeout_ms would pass
    the previous test. Drive a non-greedy prefix and assert _start_timeout
    does NOT receive 1234 for the buffering call.
    """
    processor = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=text_parser,
        app=app,
        replacement_timeout_ms=400,
        command_timeout_ms=700,
        greedy_timeout_ms=1234,
    )
    processor.context_mirror = _MockContextMirror()

    timer_calls: list[int] = []
    processor._start_timeout = lambda duration_ms: timer_calls.append(duration_ms)

    # "back" is a fresh command prefix; "back space" is a non-greedy command.
    await processor.process_word_event(
        WordEvent("back", start_of_utterance=True, end_of_utterance=False)
    )
    assert processor.mode == ProcessingMode.COMMAND_BUFFERING

    # Any _start_timeout call so far must be the standard command timer (700),
    # not the greedy timer (1234).
    assert 1234 not in timer_calls, (
        f"Non-greedy COMMAND_BUFFERING must not use the greedy timer, "
        f"but _start_timeout was called with 1234. Calls: {timer_calls}."
    )
