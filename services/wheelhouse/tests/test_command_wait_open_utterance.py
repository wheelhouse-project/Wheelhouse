"""Hold a possible command past the command wait while the utterance is
still open (wh-command-wait-open-utterance).

The reported bug: when COMMAND_TIMEOUT_MS (700 ms) expired in
COMMAND_BUFFERING while the user was still speaking, the timeout
sentinel finalized the buffer at once. "select" was typed as text, and
"left two characters" arrived a moment later and was typed too.

The change: while the utterance is still open (a real word of it was
handled and neither its utterance-end marker nor a lifecycle-reset
marker was handled yet), the sentinel holds a COMMAND_BUFFERING buffer
instead of finalizing it. The hold ends at the first of: the next word
(normal routing), the utterance end (normal end finalization), or a
deadline GREEDY_TIMEOUT_MS after the first expiry (today's timeout
result).

Every word here is sent in the shape integrations/websocket_manager.py
builds: real words carry end_of_utterance=False, and the empty
utterance-end marker closes the utterance. The processor reads the
shipped speech/config/patterns.toml.

Acceptance groups: A1 (the hold itself), A2 (the logged cases with the
measured gaps), A3 (behaviour that must not change), A4 (no utterance
end, provider change, new utterance).
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
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

PATTERNS_PATH = wheelhouse_dir / "speech" / "config" / "patterns.toml"

# The shipped defaults (config.toml.example lines 24-26).
SHIPPED_COMMAND_MS = 700
SHIPPED_REPLACEMENT_MS = 700
SHIPPED_GREEDY_MS = 5000

# Time allowed after the last event for the processor to finish.
SETTLE_S = 0.3


class RecordingApp:
    """Records every payload the processor sends to the Input process."""

    def __init__(self):
        self.actions: List[Dict[str, Any]] = []

    async def send_command(self, payload: dict):
        self.actions.append(payload)

    async def send_request(self, action: str, params: dict):
        self.actions.append({"action": action, "params": params})
        return {"status": "success"}

    def effects(self) -> List[Dict[str, Any]]:
        """Everything the user would see: typing, keys, clicks."""
        return [
            a for a in self.actions
            if a.get("action") not in ("start_utterance", "end_utterance")
        ]

    def inserted(self) -> List[str]:
        return [
            a["params"]["insertion_string"] for a in self.actions
            if a.get("action") == "intelligent_insert_text"
        ]

    def hotkeys(self) -> List[Dict[str, Any]]:
        return [
            a["params"] for a in self.actions
            if a.get("action") == "hotkey_action"
        ]

    def presses(self) -> List[Dict[str, Any]]:
        return [
            a["params"] for a in self.actions
            if a.get("action") == "press_key_action"
        ]


def _make_processor(
    app: RecordingApp,
    *,
    command_timeout_ms: int = SHIPPED_COMMAND_MS,
    replacement_timeout_ms: int = SHIPPED_REPLACEMENT_MS,
    greedy_timeout_ms: int = SHIPPED_GREEDY_MS,
) -> SpeechProcessor:
    catalog = PatternCatalog(str(PATTERNS_PATH))
    handler = MagicMock()
    handler.app = app
    return SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=TextParser(handler, catalog),
        app=app,
        replacement_timeout_ms=replacement_timeout_ms,
        command_timeout_ms=command_timeout_ms,
        greedy_timeout_ms=greedy_timeout_ms,
    )


def _word(word: str, utterance_id: int, first: bool) -> WordEvent:
    return WordEvent(
        word=word,
        start_of_utterance=first,
        end_of_utterance=False,
        utterance_id=utterance_id,
    )


def _end_marker(utterance_id: int) -> WordEvent:
    return WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=True,
        utterance_id=utterance_id,
        is_utterance_end_marker=True,
    )


def _lifecycle_reset_marker(utterance_id: int) -> WordEvent:
    """The marker integrations/websocket_manager.py:508-516 builds."""
    return WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=False,
        utterance_id=utterance_id,
        is_lifecycle_reset_marker=True,
    )


class Speaker:
    """Feeds words to a RUNNING processor through its word queue."""

    def __init__(self, processor: SpeechProcessor):
        self.processor = processor
        self.utterance_id = 0
        self._opened = False

    async def say(self, text: str, *, new_utterance: bool = False):
        if new_utterance or not self._opened:
            self.utterance_id += 1
            self._opened = False
        for word in text.split():
            await self.processor.word_queue.put(
                _word(word, self.utterance_id, first=not self._opened)
            )
            self._opened = True
        # Let the processing loop take the words before the caller waits.
        await asyncio.sleep(0.02)

    async def end(self):
        await self.processor.word_queue.put(_end_marker(self.utterance_id))
        self._opened = False


async def _run_script(script, **timeouts) -> RecordingApp:
    """Run a script of words, pauses (seconds) and "END" markers."""
    app = RecordingApp()
    processor = _make_processor(app, **timeouts)
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
    return app


# ============================================================================
# A2: the logged cases, with the measured gaps and the shipped timeouts
# ============================================================================

class TestLoggedCasesWithMeasuredGaps:
    """wheelhouse.log 2026-09-26, trace T-17904526149 and the timeline
    listed in the bead. Each gap is longer than COMMAND_TIMEOUT_MS
    (700 ms), so before the change the timer cut every one of them."""

    async def test_select_pause_left_two_characters_selects(self):
        """15:56:57: "select" + 0.87 s + "left two characters"."""
        app = await _run_script(
            ["select", 0.87, "left two characters", "END"]
        )
        assert app.hotkeys() == [
            {"keys": ["shift", "left"], "repeat": 2}
        ], app.actions
        assert app.inserted() == [], app.actions

    async def test_select_three_pause_words_to_the_left_is_not_split(self):
        """15:24:24: "select three" + 1.25 s + "words to the left".

        No shipped pattern matches "select three words to the left"
        (only select-phrase, which needs the hotword), so the same words
        said WITHOUT a pause dictate as one piece. The pause must give
        the same result, not "select three" plus four single words.
        """
        app = await _run_script(
            ["select three", 1.25, "words to the left", "END"]
        )
        no_pause = await _run_script(
            ["select three words to the left", "END"]
        )
        assert app.inserted() == ["select three words to the left"], (
            app.actions
        )
        assert app.effects() == no_pause.effects(), (
            app.actions, no_pause.actions,
        )

    async def test_select_forward_three_pause_words_selects(self):
        """16:13:22: "select forward three" + 0.84 s + "words"."""
        app = await _run_script(
            ["select forward three", 0.84, "words", "END"]
        )
        assert app.hotkeys() == [
            {"keys": ["shift", "ctrl", "right"], "repeat": 3}
        ], app.actions
        assert app.inserted() == [], app.actions

    async def test_backspace_space_pause_bar_is_backspace_then_space_bar(
        self,
    ):
        """15:22:04: "backspace space" + about 0.7 s + "bar".

        "Backspace Space Bar" is the backspace command followed by the
        space-bar replacement (patterns.toml doc_id punct-space-bar).
        Before the change the timer ran "backspace" and typed the word
        "space", then typed "bar". The pause must give the unbroken
        result: one backspace press, then one space character.
        """
        app = await _run_script(
            ["backspace space", 0.75, "bar", "END"]
        )
        no_pause = await _run_script(["backspace space bar", "END"])
        assert app.presses() == [{"key": "backspace", "repeat": 1}], (
            app.actions
        )
        assert app.inserted() == [" "], app.actions
        assert app.effects() == no_pause.effects(), (
            app.actions, no_pause.actions,
        )


# ============================================================================
# A1: the hold itself
# ============================================================================

class TestTheHold:

    async def test_nothing_is_typed_or_executed_during_the_hold(self):
        app = RecordingApp()
        processor = _make_processor(
            app, command_timeout_ms=100, greedy_timeout_ms=3000,
        )
        await processor.start()
        speaker = Speaker(processor)
        try:
            await speaker.say("select")
            # Four command timeouts, all inside the open utterance.
            await asyncio.sleep(0.4)
            assert app.effects() == [], app.actions
            assert processor.mode == ProcessingMode.COMMAND_BUFFERING
            assert processor.buffer == ["select"]
            # The rest of the command arrives; it runs, nothing is typed.
            await speaker.say("left two characters")
            await speaker.end()
            await asyncio.sleep(SETTLE_S)
        finally:
            await processor.stop()
        assert app.hotkeys() == [
            {"keys": ["shift", "left"], "repeat": 2}
        ], app.actions
        assert app.inserted() == [], app.actions

    async def test_deadline_counts_from_first_expiry_and_is_not_extended(
        self, monkeypatch,
    ):
        """Driven by hand, with a fake clock, so the arithmetic is exact.

        The processor is not started: each event is handed straight to
        process_word_event, and each timer expiry is a sentinel built
        with the processor's current token. The clock is the module
        global ``time`` in speech_processor, replaced for this test
        only; asyncio keeps the real clock.
        """
        sp_module = sys.modules[SpeechProcessor.__module__]

        clock = [100.0]
        monkeypatch.setattr(
            sp_module, "time",
            SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
        )
        app = RecordingApp()
        processor = _make_processor(
            app, command_timeout_ms=700, greedy_timeout_ms=5000,
        )
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

        try:
            await processor.process_word_event(_word("select", 1, True))
            assert armed == [700]

            # First expiry at t=100.0: hold, deadline 105.0.
            clock[0] = 100.0
            await expire()
            assert app.effects() == [], app.actions
            assert armed[-1] == 5000, armed

            # A later word re-arms the ordinary command timer; its
            # expiry re-arms only for the time left to 105.0.
            clock[0] = 102.0
            await processor.process_word_event(_word("left", 1, False))
            assert armed[-1] == 700, armed
            clock[0] = 102.7
            await expire()
            assert app.effects() == [], app.actions
            assert abs(armed[-1] - 2300) <= 1, armed

            clock[0] = 104.0
            await processor.process_word_event(_word("two", 1, False))
            clock[0] = 104.7
            await expire()
            assert app.effects() == [], app.actions
            assert abs(armed[-1] - 300) <= 1, armed

            # At the deadline the sentinel finalizes the buffer exactly
            # as today's timeout does.
            clock[0] = 105.0
            await expire()
            assert app.inserted() == ["select left two"], app.actions
            assert processor.mode == ProcessingMode.IDLE
        finally:
            await processor.stop()

    async def test_a_new_buffer_gets_a_fresh_deadline(self, monkeypatch):
        """The deadline belongs to one buffer: once that buffer
        finalizes, the next command buffer starts its own full wait.

        A command buffer opens only on an utterance's first word
        (SpeechRouter.decide routes a mid-utterance "select" through
        _decide_idle as dictation), so the next buffer is the next
        utterance's.
        """
        sp_module = sys.modules[SpeechProcessor.__module__]

        clock = [200.0]
        monkeypatch.setattr(
            sp_module, "time",
            SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
        )
        app = RecordingApp()
        processor = _make_processor(
            app, command_timeout_ms=700, greedy_timeout_ms=5000,
        )
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

        try:
            await processor.process_word_event(_word("select", 1, True))
            await expire()                     # hold, deadline 205.0
            clock[0] = 205.0
            await expire()                     # deadline: "select" typed
            assert app.inserted() == ["select"], app.actions

            await processor.process_word_event(_end_marker(1))
            clock[0] = 206.0
            await processor.process_word_event(_word("select", 2, True))
            await expire()                     # new buffer: full wait
            assert app.inserted() == ["select"], app.actions
            assert armed[-1] == 5000, armed
        finally:
            await processor.stop()

    async def test_a_remainder_under_one_ms_rearms_one_ms_not_zero(
        self, monkeypatch,
    ):
        """The re-armed timer is rounded UP, so a remainder under 1 ms
        re-arms for 1 ms: the next expiry lands at or after the deadline.
        Rounding down would re-arm for 0 ms, before the deadline."""
        clock = [100.0]
        app, processor, armed, expire = _driven(monkeypatch, clock)
        try:
            await processor.process_word_event(_word("select", 1, True))
            await expire()                     # hold, deadline 105.0
            clock[0] = 104.2
            await processor.process_word_event(_word("left", 1, False))
            assert armed[-1] == 700, armed
            clock[0] = 104.9995                # 0.5 ms before the deadline
            await expire()
            assert app.effects() == [], app.actions
            assert processor.mode == ProcessingMode.COMMAND_BUFFERING
            assert armed[-1] == 1, armed
        finally:
            await processor.stop()


def _driven(monkeypatch, clock, app=None):
    """A processor driven by hand with a fake clock, as in TestTheHold:
    shipped timers (700 ms command, 5000 ms greedy and hold). Returns
    (app, processor, armed timer durations, expire)."""
    sp_module = sys.modules[SpeechProcessor.__module__]
    monkeypatch.setattr(
        sp_module, "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
    )
    app = app if app is not None else RecordingApp()
    processor = _make_processor(
        app, command_timeout_ms=700, greedy_timeout_ms=5000,
    )
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

    return app, processor, armed, expire


class _RaiseOnce:
    """Stands in for a router method: raises on the first call, then
    delegates to the real method."""

    def __init__(self, real):
        self.real = real
        self.raised = False

    def __call__(self, *args, **kwargs):
        if not self.raised:
            self.raised = True
            raise RuntimeError("injected failure")
        return self.real(*args, **kwargs)


class TestAnEmptiedBufferLeavesNoDeadline:
    """Each path that throws a held command buffer away must also drop
    its deadline. Otherwise the next command buffer, built after the old
    deadline passed, finalizes at its first expiry instead of getting
    its own full hold.

    Shape of each test: "select" is held (deadline 105.0); the path
    under test throws that buffer away; a new "select" buffer is built;
    its command timer expires at 106.0, after the old deadline. The new
    buffer must be held with a fresh deadline of 111.0.
    """

    @staticmethod
    async def _hold_select(processor, expire):
        await processor.process_word_event(_word("select", 1, True))
        await expire()
        assert processor.mode == ProcessingMode.COMMAND_BUFFERING
        assert processor._command_hold_deadline == 105.0

    @staticmethod
    def _assert_fresh_hold(app, processor, armed):
        # The retract request itself is not typing; nothing else may be.
        typed = [a for a in app.effects() if a.get("action") != "retract"]
        assert typed == [], app.actions
        assert processor.mode == ProcessingMode.COMMAND_BUFFERING
        assert processor.buffer == ["select"]
        assert processor._command_hold_deadline == 111.0
        assert armed[-1] == 5000, armed

    async def test_after_a_retraction_replay(self, monkeypatch):
        """The retraction discards the held buffer, and its replay of
        the corrected final builds a new one."""

        class NothingPastedApp(RecordingApp):
            async def send_request(self, action, params):
                self.actions.append({"action": action, "params": params})
                if action == "retract":
                    return {"status": "not_retracted",
                            "reason": "nothing_to_retract"}
                return {"status": "success"}

        clock = [100.0]
        app, processor, armed, expire = _driven(
            monkeypatch, clock, app=NothingPastedApp(),
        )
        try:
            await self._hold_select(processor, expire)
            clock[0] = 101.0
            await processor.process_word_event(WordEvent(
                word="", start_of_utterance=False, end_of_utterance=False,
                utterance_id=1, is_retraction_marker=True,
                retraction_full_text="select",
            ))
            assert processor.mode == ProcessingMode.COMMAND_BUFFERING
            assert armed[-1] == 700, armed
            clock[0] = 106.0
            await expire()
            self._assert_fresh_hold(app, processor, armed)
        finally:
            await processor.stop()

    async def test_after_a_lifecycle_reset_whose_close_out_fails(
        self, monkeypatch,
    ):
        """The finalization at the lifecycle-reset marker raises; the
        marker's own reset throws the buffer away. Phrase 2 reuses the
        utterance id."""
        clock = [100.0]
        app, processor, armed, expire = _driven(monkeypatch, clock)
        try:
            await self._hold_select(processor, expire)
            failing = _RaiseOnce(processor.router.decide_timeout)
            monkeypatch.setattr(processor.router, "decide_timeout", failing)
            clock[0] = 101.0
            await processor.process_word_event(_lifecycle_reset_marker(1))
            assert failing.raised
            assert processor.mode == ProcessingMode.IDLE
            await processor.process_word_event(_word("select", 1, True))
            clock[0] = 106.0
            await expire()
            self._assert_fresh_hold(app, processor, armed)
        finally:
            await processor.stop()

    async def test_after_a_word_processing_error(self, monkeypatch):
        """A word raises inside the running processing loop; the loop's
        error handler throws the buffer away. Real timers are 700 ms and
        5000 ms, and each check below comes well inside 700 ms, so every
        expiry here is the injected one."""
        clock = [100.0]
        app, processor, armed, _ = _driven(monkeypatch, clock)
        queue = processor.word_queue

        async def put(event):
            await queue.put(event)
            await asyncio.sleep(0.05)

        async def expire():
            await put(WordEvent.timeout_finalize(
                token=processor.timeout_token
            ))

        await processor.start()
        try:
            await put(_word("select", 1, True))
            await expire()
            assert processor.mode == ProcessingMode.COMMAND_BUFFERING
            assert processor._command_hold_deadline == 105.0

            failing = _RaiseOnce(processor.router.decide)
            monkeypatch.setattr(processor.router, "decide", failing)
            clock[0] = 101.0
            await put(_word("left", 1, False))
            assert failing.raised
            assert processor.mode == ProcessingMode.IDLE

            await put(_word("select", 2, True))
            clock[0] = 106.0
            await expire()
            self._assert_fresh_hold(app, processor, armed)
        finally:
            await processor.stop()


class TestOnlyTheCommandTimerStartsTheHold:
    """A1 names the command timer (COMMAND_TIMEOUT_MS). A timer that was
    already GREEDY_TIMEOUT_MS long, which the router arms for a greedy
    command such as press-key, ``^press\\s*(.+)$``, has given the user
    the full greedy wait: its expiry finalizes as today, with no second
    greedy wait.

    Driven by hand with a fake clock, as in TestTheHold.
    """

    @staticmethod
    def _driven(monkeypatch, clock):
        sp_module = sys.modules[SpeechProcessor.__module__]
        monkeypatch.setattr(
            sp_module, "time",
            SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
        )
        app = RecordingApp()
        processor = _make_processor(
            app, command_timeout_ms=700, greedy_timeout_ms=5000,
        )
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

        return app, processor, armed, expire

    async def test_a_command_length_expiry_holds(self, monkeypatch):
        clock = [300.0]
        app, processor, armed, expire = self._driven(monkeypatch, clock)
        try:
            await processor.process_word_event(_word("select", 1, True))
            assert armed == [700], armed
            await expire()
            assert app.effects() == [], app.actions
            assert processor.buffer == ["select"]
            assert armed == [700, 5000], armed
        finally:
            await processor.stop()

    async def test_a_greedy_length_expiry_finalizes_at_once(
        self, monkeypatch,
    ):
        clock = [400.0]
        app, processor, armed, expire = self._driven(monkeypatch, clock)
        try:
            await processor.process_word_event(_word("press", 1, True))
            await processor.process_word_event(_word("enter", 1, False))
            assert processor.mode == ProcessingMode.COMMAND_BUFFERING
            assert armed[-1] == 5000, armed
            timers_before = len(armed)
            clock[0] = 405.0
            await expire()
            # Finalized at this expiry: no timer re-armed, buffer gone.
            assert len(armed) == timers_before, armed
            assert processor.mode == ProcessingMode.IDLE
            assert app.hotkeys() == [
                {"keys": ["enter"], "repeat": 1}
            ], app.actions
        finally:
            await processor.stop()


# ============================================================================
# A4: no utterance end, provider change, new utterance
# ============================================================================

class TestNoUtteranceEnd:

    @pytest.mark.parametrize(
        "spoken, expected",
        [
            pytest.param(
                "select",
                [{"action": "intelligent_insert_text",
                  "params": {"insertion_string": "select"}}],
                id="select",
            ),
            pytest.param(
                "backspace space",
                [{"action": "press_key_action",
                  "params": {"key": "backspace", "repeat": 1}},
                 {"action": "intelligent_insert_text",
                  "params": {"insertion_string": "space"}}],
                id="backspace-space",
            ),
        ],
    )
    async def test_held_words_are_handled_at_the_greedy_deadline(
        self, spoken, expected,
    ):
        """A dropped final: no utterance end ever arrives.

        ``expected`` is what today's timeout produces for these words
        (measured on the unchanged code at 599ff942). The hold must end
        with exactly that, one greedy wait after the first expiry
        (100 ms + 400 ms here).
        """
        app = RecordingApp()
        processor = _make_processor(
            app, command_timeout_ms=100, greedy_timeout_ms=400,
        )
        await processor.start()
        speaker = Speaker(processor)
        try:
            await speaker.say(spoken)
            await asyncio.sleep(0.3)
            held = list(app.effects())
            await asyncio.sleep(0.6)
        finally:
            await processor.stop()
        assert held == [], app.actions
        assert app.effects() == expected, app.actions


class TestTheHoldMeetsANewUtterance:

    async def test_a_new_utterance_during_the_hold_keeps_the_held_words(
        self,
    ):
        app = RecordingApp()
        processor = _make_processor(
            app, command_timeout_ms=100, greedy_timeout_ms=3000,
        )
        await processor.start()
        speaker = Speaker(processor)
        try:
            await speaker.say("select")
            await asyncio.sleep(0.3)
            held = list(app.effects())
            # The previous utterance's end marker was lost; the next
            # utterance opens.
            await speaker.say("hello", new_utterance=True)
            await speaker.end()
            await asyncio.sleep(SETTLE_S)
        finally:
            await processor.stop()
        assert held == [], app.actions
        assert app.inserted() == ["select", "hello"], app.actions

    async def test_provider_switch_then_new_engine_word_keeps_held_words(
        self,
    ):
        """The switch in main.py drops only a held bare number
        (drop_bare_number_hold); the new engine's first word opens a
        new utterance and finalizes the held command buffer."""
        app = RecordingApp()
        processor = _make_processor(
            app, command_timeout_ms=100, greedy_timeout_ms=3000,
        )
        await processor.start()
        speaker = Speaker(processor)
        try:
            await speaker.say("select")
            await asyncio.sleep(0.3)
            held = list(app.effects())
            processor.drop_bare_number_hold("stt provider switch")
            await speaker.say("hello", new_utterance=True)
            await speaker.end()
            await asyncio.sleep(SETTLE_S)
        finally:
            await processor.stop()
        assert held == [], app.actions
        assert app.inserted() == ["select", "hello"], app.actions

    async def test_old_engine_disconnect_end_marker_releases_held_words(
        self,
    ):
        """When the old engine was the last client, WebSocketManager
        queues a cleanup end marker for the utterance in progress
        (integrations/websocket_manager.py:1870-1882). The held words
        finalize there, at once, instead of waiting for the deadline."""
        app = RecordingApp()
        processor = _make_processor(
            app, command_timeout_ms=100, greedy_timeout_ms=3000,
        )
        await processor.start()
        speaker = Speaker(processor)
        try:
            await speaker.say("select")
            await asyncio.sleep(0.3)
            held = list(app.effects())
            await speaker.end()
            await asyncio.sleep(SETTLE_S)
        finally:
            await processor.stop()
        assert held == [], app.actions
        assert app.inserted() == ["select"], app.actions


# ============================================================================
# A3: behaviour that must not change
# ============================================================================

class TestUnchanged:

    async def test_a_buffer_the_next_word_rules_out_dictates_as_today(self):
        """No pause: the next word decides through normal routing.
        "select the best option" matches only select-phrase, which needs
        the hotword, so it is dictation, typed at the utterance end."""
        app = await _run_script(["select the best option", "END"])
        assert app.inserted() == ["select the best option"], app.actions
        assert app.hotkeys() == [], app.actions

    @pytest.mark.parametrize(
        "spoken, inserted, hotkeys",
        [
            pytest.param("select", ["select"], [], id="dictates"),
            pytest.param(
                "select left two characters", [],
                [{"keys": ["shift", "left"], "repeat": 2}], id="executes",
            ),
        ],
    )
    async def test_the_utterance_end_finalizes_as_today(
        self, spoken, inserted, hotkeys,
    ):
        """The end marker finalizes a possible command at once, even
        with a 5000 ms greedy wait configured."""
        app = await _run_script(
            [spoken, "END"],
            command_timeout_ms=100, greedy_timeout_ms=5000,
        )
        assert app.inserted() == inserted, app.actions
        assert app.hotkeys() == hotkeys, app.actions

    @pytest.mark.parametrize(
        "closing_marker",
        [_end_marker, _lifecycle_reset_marker],
        ids=["utterance-end", "lifecycle-reset"],
    )
    async def test_an_expiry_after_the_utterance_closed_is_not_held(
        self, closing_marker,
    ):
        """After the utterance-end or lifecycle-reset marker, the
        utterance is no longer open, so an expiry finalizes at once.

        The first sentinel is the ordinary one: the closing marker
        already finalized the buffer, so the sentinel meets IDLE and is
        ignored. The second puts a command buffer back by hand (a state
        no word produced) to show the closed utterance, not the mode,
        is what stops the hold.
        """
        app = RecordingApp()
        processor = _make_processor(
            app, command_timeout_ms=700, greedy_timeout_ms=5000,
        )
        try:
            await processor.process_word_event(_word("select", 1, True))
            await processor.process_word_event(closing_marker(1))
            assert app.inserted() == ["select"], app.actions
            await processor.process_word_event(
                WordEvent.timeout_finalize(token=processor.timeout_token)
            )
            assert app.inserted() == ["select"], app.actions

            processor.mode = ProcessingMode.COMMAND_BUFFERING
            processor.buffer = ["select"]
            await processor.process_word_event(
                WordEvent.timeout_finalize(token=processor.timeout_token)
            )
            assert app.inserted() == ["select", "select"], app.actions
            assert processor.mode == ProcessingMode.IDLE
        finally:
            await processor.stop()

    async def test_replacement_prefix_hold_completes_a_paused_name(self):
        """"open" buffers as a command AND opens a punctuation name, so
        it keeps the replacement-prefix hold (wh-spaced-punctuation-
        names-unresolved.3). The pipeline version of this case is
        tests/test_pipeline_punctuation_names.py::TestAPauseBetweenTheWords
        ::test_the_pause_inside_one_streaming_utterance_still_works."""
        app = await _run_script(
            ["open", 0.3, "bracket", "END"],
            command_timeout_ms=100, replacement_timeout_ms=400,
            greedy_timeout_ms=3000,
        )
        assert app.inserted() == ["["], app.actions

    async def test_replacement_prefix_release_window_is_unchanged(self):
        """The held opening is typed when its release window
        (replacement_timeout_ms after the command expiry) ends, not at
        the greedy deadline."""
        app = RecordingApp()
        processor = _make_processor(
            app, command_timeout_ms=100, replacement_timeout_ms=100,
            greedy_timeout_ms=3000,
        )
        await processor.start()
        speaker = Speaker(processor)
        try:
            await speaker.say("open")
            await asyncio.sleep(0.6)
            typed = list(app.inserted())
        finally:
            await processor.stop()
        assert typed == ["open"], app.actions

    async def test_hotword_buffering_is_not_held(self):
        """A HOTWORD_BUFFERING buffer whose GREEDY timer expires
        finalizes as today, even in an open utterance.
        (wh-hotword-wait-open-utterance holds a hotword buffer after a
        COMMAND-length expiry; tests/test_hotword_wait_open_utterance.py
        covers that.)

        "x-ray select" arms the greedy timer (400 ms here), because the
        hotword makes select-phrase, ``^select (.+)$``, a candidate. A
        hold would add a second 400 ms wait after that expiry; the
        check at 600 ms sits between the two.
        """
        app = RecordingApp()
        processor = _make_processor(
            app, command_timeout_ms=100, greedy_timeout_ms=400,
        )
        await processor.start()
        speaker = Speaker(processor)
        try:
            await speaker.say("x-ray select")
            assert processor.mode == ProcessingMode.HOTWORD_BUFFERING
            await asyncio.sleep(0.6)
            typed = list(app.inserted())
        finally:
            await processor.stop()
        assert typed == ["x-ray select"], app.actions


# ============================================================================
# The hold length: open_utterance_hold_ms (boss ruling 18:44)
# ============================================================================

def _bare_processor(app: RecordingApp, **kwargs) -> SpeechProcessor:
    """A SpeechProcessor built the way speech_handler.py builds it: no
    open_utterance_hold_ms argument unless a test passes one."""
    catalog = PatternCatalog(str(PATTERNS_PATH))
    handler = MagicMock()
    handler.app = app
    return SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=TextParser(handler, catalog),
        app=app,
        replacement_timeout_ms=700,
        command_timeout_ms=700,
        greedy_timeout_ms=5000,
        **kwargs,
    )


async def _select_then_expire(monkeypatch, processor):
    """Say "select" (command timer 700 ms) and fire that timer once.
    Returns every timer duration the processor armed."""
    armed: List[int] = []
    real_start = processor._start_timeout

    def spy(duration_ms):
        armed.append(duration_ms)
        real_start(duration_ms)

    monkeypatch.setattr(processor, "_start_timeout", spy)
    await processor.process_word_event(_word("select", 1, True))
    await processor.process_word_event(
        WordEvent.timeout_finalize(token=processor.timeout_token)
    )
    return armed


class TestTheHoldLength:

    async def test_the_default_hold_is_the_greedy_wait_not_zero(
        self, monkeypatch,
    ):
        """Fails if the default hold becomes 0: a processor built with
        no open_utterance_hold_ms argument (as speech_handler.py builds
        it) must hold after a command-length expiry."""
        app = RecordingApp()
        processor = _bare_processor(app)
        try:
            armed = await _select_then_expire(monkeypatch, processor)
            assert app.effects() == [], app.actions
            assert processor.mode == ProcessingMode.COMMAND_BUFFERING
            assert armed == [700, 5000], armed
        finally:
            await processor.stop()

    async def test_zero_is_no_hold(self, monkeypatch):
        """0 is the pre-fix behaviour: the command timer finalizes."""
        app = RecordingApp()
        processor = _bare_processor(app, open_utterance_hold_ms=0)
        try:
            armed = await _select_then_expire(monkeypatch, processor)
            assert app.inserted() == ["select"], app.actions
            assert processor.mode == ProcessingMode.IDLE
            assert armed == [700], armed
        finally:
            await processor.stop()

    async def test_the_hold_uses_the_given_length(self, monkeypatch):
        app = RecordingApp()
        processor = _bare_processor(app, open_utterance_hold_ms=1234)
        try:
            armed = await _select_then_expire(monkeypatch, processor)
            assert app.effects() == [], app.actions
            assert armed[0] == 700 and abs(armed[-1] - 1234) <= 1, armed
        finally:
            await processor.stop()


# ============================================================================
# The default hold through the real e2e pipeline (UIActionHandler, OS mocks)
# ============================================================================

from services.wheelhouse.tests.e2e.e2e_harness import E2EPipelineHarness


class TestTheDefaultHoldThroughTheE2EPipeline:
    """E2EPipelineHarness built with the default hold (the shared
    harness passes 0 unless told otherwise). Its command timer is
    1000 ms."""

    async def test_hold_then_end_marker_runs_the_command(self):
        harness = E2EPipelineHarness(open_utterance_hold_ms=None)
        await harness.start()
        try:
            await harness.send_word("select", start_of_utterance=True)
            await asyncio.sleep(1.3)           # the 1000 ms timer expired
            held_keys = list(harness.recording.get_keystroke_keys())
            held_text = list(harness.recording.clipboard_pastes)
            for word in ("left", "two", "characters"):
                await harness.send_word(word)
            await harness.send_utterance_end_marker(1)
            await asyncio.sleep(SETTLE_S)
        finally:
            await harness.stop()
        assert held_keys == [] and held_text == []
        keys = harness.recording.get_keystroke_keys()
        assert keys and all(
            "shift" in str(k) and "left" in str(k) for k in keys
        ), keys
        assert harness.recording.clipboard_pastes == []
        assert harness.recording.typed_texts == []

    async def test_hold_then_deadline_types_the_words(self):
        """No end marker. greedy_timeout_ms 1500 in this instance only:
        the 1000 ms timer expires at about 1.0 s, the hold ends at about
        2.5 s and types the word as today's timeout would."""
        harness = E2EPipelineHarness(
            greedy_timeout_ms=1500, open_utterance_hold_ms=None,
        )
        await harness.start()
        try:
            await harness.send_word("select", start_of_utterance=True)
            await asyncio.sleep(1.8)
            held_text = list(harness.recording.clipboard_pastes)
            await asyncio.sleep(1.2)
        finally:
            await harness.stop()
        assert held_text == []
        # The real insert path capitalizes the first word it delivers.
        assert harness.recording.clipboard_pastes == ["Select"], (
            harness.recording.clipboard_pastes
        )


# ============================================================================
# Badges showing, default hold: only a confirmed end clicks
# ============================================================================

from services.wheelhouse.click_overlay_state import OverlayState
from test_grid_number_badge_click import _FakeLogicController, _RecordingApp


class TestBadgeNumberWithTheDefaultHold:
    """The badge stack of tests/test_grid_number_badge_click.py, built
    with the default hold (its _make_stack passes 0). "one twelve"
    names badge 112. Driven by hand with a fake clock."""

    @staticmethod
    def _stack(monkeypatch, clock):
        sp_module = sys.modules[SpeechProcessor.__module__]
        monkeypatch.setattr(
            sp_module, "time",
            SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
        )
        catalog = PatternCatalog(str(PATTERNS_PATH))
        lc = _FakeLogicController(OverlayState.PAINTED, False)
        speech_handler = MagicMock()
        speech_handler.logic_controller = lc
        app = _RecordingApp()
        speech_handler.app = app
        processor = SpeechProcessor(
            word_queue=asyncio.Queue(),
            catalog=catalog,
            text_parser=TextParser(speech_handler, catalog),
            app=app,
            logic_controller=lc,
        )
        speech_handler.speech_processor = processor
        return processor, app, lc

    @staticmethod
    async def _say_one_twelve_then_expire(processor):
        await processor.process_word_event(_word("one", 1, True))
        await processor.process_word_event(_word("twelve", 1, False))
        await processor.process_word_event(
            WordEvent.timeout_finalize(token=processor.timeout_token)
        )

    async def test_a_hold_then_a_confirmed_end_clicks_badge_112(
        self, monkeypatch,
    ):
        clock = [600.0]
        processor, app, lc = self._stack(monkeypatch, clock)
        try:
            await self._say_one_twelve_then_expire(processor)
            assert lc.clicks == [] and app.inserted_texts() == []
            assert processor.mode == ProcessingMode.COMMAND_BUFFERING
            await processor.process_word_event(_end_marker(1))
        finally:
            await processor.stop()
        assert app.inserted_texts() == []
        assert len(lc.clicks) == 1
        assert lc.clicks[0].name == "one twelve"

    async def test_a_hold_with_no_end_types_the_words_at_the_deadline(
        self, monkeypatch,
    ):
        clock = [700.0]
        processor, app, lc = self._stack(monkeypatch, clock)
        try:
            await self._say_one_twelve_then_expire(processor)
            assert lc.clicks == [] and app.inserted_texts() == []
            clock[0] = 705.0                   # the 5000 ms deadline
            await processor.process_word_event(
                WordEvent.timeout_finalize(token=processor.timeout_token)
            )
        finally:
            await processor.stop()
        assert lc.clicks == []
        assert app.inserted_texts() == ["one twelve"]
