"""Hold the x-ray hotword state past the command wait while the utterance
is still open (wh-hotword-wait-open-utterance).

The reported bug: "x-ray click personalization" did not click anything.
"x-ray" put the processor in HOTWORD_BUFFERING and armed the 700 ms
command timer. Parakeet delivered "click" 906 ms later (1538 ms in a
second case), so the timer expired first, the empty hotword buffer was
dropped, and "click personalization" was typed as text
(wheelhouse.log 2026-09-27, UTT-203 and UTT-204).

The change extends the command wait of wh-command-wait-open-utterance
(tests/test_command_wait_open_utterance.py) to HOTWORD_BUFFERING,
including the empty buffer after the hotword alone. The hold ends at the
first of: the next word (normal routing), the utterance end (normal end
finalization), or open_utterance_hold_ms after the first expiry (today's
timeout result).

Acceptance groups: A1 (the hold itself), A2 (the logged cases with the
measured gaps), A3 (behaviour that must not change), A4 (no utterance
end).
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest.mock import MagicMock

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import pytest

from speech.command_engine import TextParser
from speech.domain import ProcessingMode
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from speech.word_event import WordEvent
from test_command_wait_open_utterance import (
    PATTERNS_PATH,
    SETTLE_S,
    RecordingApp,
    Speaker,
    _end_marker,
    _word,
)


class ClickRecorder:
    """Stands in for LogicController: records every click_element query
    (speech/actions.py click_element awaits forward_click_element)."""

    def __init__(self):
        self.names: List[str] = []

    async def forward_click_element(self, query, trace_id, **kwargs):
        self.names.append(query.name)


def _make_processor(app, clicks, **kwargs) -> SpeechProcessor:
    """The shipped timers unless a test passes others: 700 ms command,
    700 ms replacement, 5000 ms greedy; the hold defaults to greedy."""
    catalog = PatternCatalog(str(PATTERNS_PATH))
    handler = MagicMock()
    handler.app = app
    handler.logic_controller = clicks
    kwargs.setdefault("replacement_timeout_ms", 700)
    kwargs.setdefault("command_timeout_ms", 700)
    kwargs.setdefault("greedy_timeout_ms", 5000)
    return SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=TextParser(handler, catalog),
        app=app,
        **kwargs,
    )


async def _run_script(script, **kwargs):
    """Run a script of words, pauses (seconds) and "END" markers.
    Returns (app, clicks)."""
    app = RecordingApp()
    clicks = ClickRecorder()
    processor = _make_processor(app, clicks, **kwargs)
    await processor.start()
    speaker = Speaker(processor)
    try:
        for item in script:
            if isinstance(item, (int, float)):
                await asyncio.sleep(item)
            elif item == "END":
                await speaker.end()
            else:
                await speaker.say(item)
        await asyncio.sleep(SETTLE_S)
    finally:
        await processor.stop()
    return app, clicks


def _driven(monkeypatch, clock, **kwargs):
    """A processor driven by hand with a fake clock (the module global
    ``time`` in speech_processor; asyncio keeps the real clock). Returns
    (app, clicks, processor, armed timer durations, expire)."""
    sp_module = sys.modules[SpeechProcessor.__module__]
    monkeypatch.setattr(
        sp_module, "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
    )
    app = RecordingApp()
    clicks = ClickRecorder()
    processor = _make_processor(app, clicks, **kwargs)
    armed: List[int] = []
    real_start = processor._start_timeout

    def spy(duration_ms):
        armed.append(duration_ms)
        real_start(duration_ms)

    monkeypatch.setattr(processor, "_start_timeout", spy)

    async def expire():
        await processor.process_word_event(
            WordEvent.timeout_finalize(token=processor.timeout_token)
        )

    return app, clicks, processor, armed, expire


# ============================================================================
# A2: the logged cases, with the measured gaps and the shipped timeouts
# ============================================================================

class TestLoggedCasesWithMeasuredGaps:
    """wheelhouse.log 2026-09-27, UTT-203 and UTT-204. Each gap is longer
    than COMMAND_TIMEOUT_MS (700 ms), so before the change the timer
    dropped the hotword and "click" and "personalization" were typed."""

    async def test_x_ray_906_ms_click_personalization_clicks(self):
        """UTT-203: 'click' arrived 906 ms after 'x-ray'."""
        app, clicks = await _run_script(
            ["x-ray", 0.906, "click", "personalization", "END"]
        )
        assert clicks.names == ["personalization"], app.actions
        assert app.effects() == [], app.actions

    async def test_x_ray_1538_ms_click_personalization_clicks(self):
        """UTT-204: 'click' arrived 1538 ms after 'x-ray'."""
        app, clicks = await _run_script(
            ["x-ray", 1.538, "click personalization", "END"]
        )
        assert clicks.names == ["personalization"], app.actions
        assert app.effects() == [], app.actions

    async def test_x_ray_click_pause_personalization_clicks(self):
        app, clicks = await _run_script(
            ["x-ray click", 0.9, "personalization", "END"]
        )
        assert clicks.names == ["personalization"], app.actions
        assert app.effects() == [], app.actions


# ============================================================================
# A1: the hold itself
# ============================================================================

class TestTheHotwordHold:

    async def test_hotword_alone_is_held_not_dropped(self, monkeypatch):
        """"x-ray" alone arms the command-length timer (router.py, fresh
        hotword). Its expiry in an open utterance keeps the hotword
        state and re-arms for the hold."""
        clock = [500.0]
        app, clicks, processor, armed, expire = _driven(monkeypatch, clock)
        try:
            await processor.process_word_event(_word("x-ray", 1, True))
            assert processor.mode == ProcessingMode.HOTWORD_BUFFERING
            assert armed == [700], armed
            await expire()
            assert processor.mode == ProcessingMode.HOTWORD_BUFFERING
            assert processor.hotword_active
            assert processor.buffer == []
            assert armed == [700, 5000], armed
            assert app.effects() == [] and clicks.names == []
        finally:
            await processor.stop()

    async def test_held_words_after_the_hotword_are_kept(self, monkeypatch):
        """'x-ray click' then an expiry: 'click' is neither typed nor
        dropped, and the next word completes the command."""
        clock = [600.0]
        app, clicks, processor, armed, expire = _driven(monkeypatch, clock)
        try:
            await processor.process_word_event(_word("x-ray", 1, True))
            await processor.process_word_event(_word("click", 1, False))
            assert armed[-1] == 700, armed
            await expire()
            assert processor.mode == ProcessingMode.HOTWORD_BUFFERING
            assert processor.buffer == ["click"]
            assert app.effects() == [] and clicks.names == []
            clock[0] = 601.0
            await processor.process_word_event(
                _word("personalization", 1, False)
            )
            await processor.process_word_event(_end_marker(1))
        finally:
            await processor.stop()
        assert clicks.names == ["personalization"], app.actions
        assert app.effects() == [], app.actions

    async def test_a_replacement_name_opening_after_the_hotword_is_held(
        self, monkeypatch,
    ):
        """A1 keeps any held words. "open" is an unfinished replacement
        name, which keeps a COMMAND buffer out of this hold, because the
        replacement-prefix hold owns it there. That hold never takes a
        hotword buffer (_should_hold_replacement_prefix), so the name
        test must not keep a hotword buffer out of this hold."""
        clock = [650.0]
        app, clicks, processor, armed, expire = _driven(monkeypatch, clock)
        try:
            await processor.process_word_event(_word("x-ray", 1, True))
            await processor.process_word_event(_word("open", 1, False))
            assert processor.mode == ProcessingMode.HOTWORD_BUFFERING
            assert processor.router.is_incomplete_replacement_name(["open"])
            assert armed[-1] == 700, armed
            await expire()
            assert processor.mode == ProcessingMode.HOTWORD_BUFFERING
            assert processor.buffer == ["open"]
            assert armed[-1] == 5000, armed
            assert app.effects() == [], app.actions
        finally:
            await processor.stop()

    async def test_deadline_counts_from_first_expiry(self, monkeypatch):
        """The deadline is set at the first expiry and not moved by the
        next word; at the deadline the sentinel runs today's path."""
        clock = [100.0]
        app, clicks, processor, armed, expire = _driven(monkeypatch, clock)
        try:
            await processor.process_word_event(_word("x-ray", 1, True))
            await expire()                     # hold, deadline 105.0
            assert armed[-1] == 5000, armed
            clock[0] = 103.0
            await processor.process_word_event(_word("click", 1, False))
            clock[0] = 103.7
            await expire()
            assert abs(armed[-1] - 1300) <= 1, armed
            assert processor.mode == ProcessingMode.HOTWORD_BUFFERING
            clock[0] = 105.0
            await expire()
            assert processor.mode == ProcessingMode.IDLE
        finally:
            await processor.stop()

    async def test_zero_hold_drops_the_hotword_at_the_command_timer(
        self, monkeypatch,
    ):
        """open_utterance_hold_ms=0 is the pre-fix behaviour."""
        clock = [700.0]
        app, clicks, processor, armed, expire = _driven(
            monkeypatch, clock, open_utterance_hold_ms=0,
        )
        try:
            await processor.process_word_event(_word("x-ray", 1, True))
            await expire()
            assert processor.mode == ProcessingMode.IDLE
            assert armed == [700], armed
        finally:
            await processor.stop()


# ============================================================================
# A3: behaviour that must not change
# ============================================================================

class TestUnchanged:

    async def test_x_ray_click_bold_at_once_clicks(self):
        """The passing Bold click (18:51:35): 'click' 160 ms after
        'x-ray'."""
        app, clicks = await _run_script(
            ["x-ray", 0.16, "click bold", "END"]
        )
        assert clicks.names == ["bold"], app.actions
        assert app.effects() == [], app.actions

    async def test_x_ray_then_utterance_end_does_nothing(self):
        app, clicks = await _run_script(["x-ray", "END"])
        assert app.effects() == [] and clicks.names == [], app.actions

    async def test_hotword_expiry_after_the_utterance_end_is_not_held(
        self, monkeypatch,
    ):
        """After the end marker the utterance is closed. A hotword state
        put back by hand (no word produces it) must end at its expiry,
        not re-arm."""
        clock = [800.0]
        app, clicks, processor, armed, expire = _driven(monkeypatch, clock)
        try:
            await processor.process_word_event(_word("x-ray", 1, True))
            await processor.process_word_event(_end_marker(1))
            assert processor.mode == ProcessingMode.IDLE
            processor.mode = ProcessingMode.HOTWORD_BUFFERING
            processor.hotword_active = True
            timers_before = len(armed)
            await expire()
            assert processor.mode == ProcessingMode.IDLE
            assert len(armed) == timers_before, armed
            assert app.effects() == [] and clicks.names == []
        finally:
            await processor.stop()


# ============================================================================
# A4: no utterance end (dropped final)
# ============================================================================

class TestNoUtteranceEnd:

    @pytest.mark.parametrize(
        "spoken, expected",
        [
            pytest.param("x-ray", [], id="hotword-alone"),
            pytest.param(
                "x-ray hello",
                [{"action": "intelligent_insert_text",
                  "params": {"insertion_string": "x-ray hello"}}],
                id="hotword-then-dictation",
            ),
        ],
    )
    async def test_held_state_is_handled_at_the_deadline(
        self, spoken, expected,
    ):
        """``expected`` is what today's timeout produces for these words
        (measured on the unchanged code at 466daba1). The hold must end
        with exactly that, one hold length after the first expiry
        (100 ms + 400 ms here)."""
        app = RecordingApp()
        clicks = ClickRecorder()
        processor = _make_processor(
            app, clicks, command_timeout_ms=100, greedy_timeout_ms=400,
        )
        await processor.start()
        speaker = Speaker(processor)
        try:
            await speaker.say(spoken)
            await asyncio.sleep(0.3)
            held = list(app.effects())
            held_mode = processor.mode
            await asyncio.sleep(0.6)
            final_mode = processor.mode
        finally:
            await processor.stop()
        assert held == [], app.actions
        assert held_mode == ProcessingMode.HOTWORD_BUFFERING
        assert app.effects() == expected, app.actions
        assert final_mode == ProcessingMode.IDLE
        assert clicks.names == []


# ============================================================================
# A6: dictation after a hold that began on the hotword alone
# ============================================================================

class TestDictationAfterTheHotwordAloneTimedOut:
    """wh-hotword-wait-open-utterance A6 (David 2026-09-27 20:33, "leave
    it the way it is today"). At 466daba1 the command timer dropped the
    hotword state when "x-ray" was alone in its buffer, and the words
    that came after the pause were typed as plain dictation, without
    "x-ray". The hold keeps the hotword state so a command still
    matches, but when those words become dictation they are typed as
    they were at 466daba1: without the wake word. A buffer that already
    held words when the timer first expired ("x-ray hello" said
    fluently) keeps today's "x-ray hello"."""

    async def test_906_ms_pause_then_dictation_types_without_x_ray(self):
        app, clicks = await _run_script(["x-ray", 0.906, "hello", "END"])
        assert app.inserted() == ["hello"], app.actions
        assert clicks.names == [], app.actions

    async def test_1538_ms_pause_then_dictation_types_without_x_ray(self):
        app, clicks = await _run_script(["x-ray", 1.538, "hello", "END"])
        assert app.inserted() == ["hello"], app.actions
        assert clicks.names == [], app.actions

    async def test_pause_then_dictation_with_no_end_types_without_x_ray(
        self,
    ):
        """No end marker: the hold deadline (100 ms + 400 ms after the
        first expiry here) finalizes the buffer."""
        app, clicks = await _run_script(
            ["x-ray", 0.15, "hello", 0.8],
            command_timeout_ms=100, greedy_timeout_ms=400,
        )
        assert app.inserted() == ["hello"], app.actions
        assert clicks.names == [], app.actions

    async def test_pause_then_a_new_utterance_types_without_x_ray(self):
        """The auto-finalize path: a new utterance closes the held
        buffer."""
        app = RecordingApp()
        clicks = ClickRecorder()
        processor = _make_processor(app, clicks)
        await processor.start()
        speaker = Speaker(processor)
        try:
            await speaker.say("x-ray")
            await asyncio.sleep(0.906)
            await speaker.say("hello")
            await speaker.say("world", new_utterance=True)
            await speaker.end()
            await asyncio.sleep(SETTLE_S)
        finally:
            await processor.stop()
        assert app.inserted() == ["hello", "world"], app.actions
        assert clicks.names == [], app.actions

    async def test_fluent_x_ray_hello_types_x_ray_hello(self):
        """Unchanged (tests/test_router_hotword_variants.py pins the
        router payload)."""
        app, clicks = await _run_script(["x-ray hello", "END"])
        assert app.inserted() == ["x-ray hello"], app.actions
        assert clicks.names == [], app.actions

    async def test_fluent_x_ray_hello_then_a_pause_types_x_ray_hello(self):
        """The buffer held "hello" when the command timer first expired,
        so the hold did not begin on the hotword alone. 466daba1 typed
        "x-ray hello" at that expiry."""
        app, clicks = await _run_script(["x-ray hello", 0.9, "END"])
        assert app.inserted() == ["x-ray hello"], app.actions
        assert clicks.names == [], app.actions

    async def test_pause_then_a_command_still_clicks(self):
        app, clicks = await _run_script(
            ["x-ray", 0.906, "click", "bold", "END"]
        )
        assert clicks.names == ["bold"], app.actions
        assert app.effects() == [], app.actions
