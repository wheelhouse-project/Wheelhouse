"""Tests for the screen-read dictation gate (wh-overlay-slow-uia-stale-badges.7).

A UIA screen read (an overlay walk, a settle re-read, or a by-name click
walk) blocks the ONE command loop in the Input process, so a dictation
word sent during the read queues behind it and types seconds late into
whatever window has focus by then. The Logic-side gate:

  1. REFUSES new dictation words while a read is in flight -- it drops
     them, never queues them, and no refused word types after the read
     ends (acceptance 1).
  2. Leaves spoken numbers alone: the bare-number hold runs earlier in
     the DICTATE branch and consumes as "click N" at the utterance end,
     entirely Logic-handled (acceptance 2).
  3. Makes a read requested mid-sentence wait, bounded, for the current
     sentence to end before the read is sent to Input (acceptance 3).
  4. Carries its own maximum wait, so a stuck read marker cannot refuse
     voice forever (acceptance 4).

The read-in-flight signal is ``LogicController.screen_read_in_flight_since``,
a ``time.monotonic()`` stamp set around the actual Input round trip (the
``send_request`` await), or ``None``. The mid-sentence signal is the
processor's ``sentence_closed_event`` (set = no sentence open).
"""
import sys
from pathlib import Path

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import asyncio
import re
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.wheelhouse.click_overlay_state import (
    BuildReason,
    ClickOverlayStateMachine,
    Effect,
    EffectKind,
    OverlayEvent,
    OverlayEventKind,
    OverlayState,
    PaintAckState,
)
from services.wheelhouse.main import LogicController
from services.wheelhouse.ui.click_config import ClickConfig
from services.wheelhouse.shared.start_overlay_walk import (
    StartOverlayWalkResponse,
)
from services.wheelhouse.ui.element_types import (
    ElementQuery,
    WalkSnapshotSummary,
    WalkSnapshotSummaryItem,
)
from speech import speech_processor as sp_module
from speech.command_engine import TextParser
from speech.word_event import WordEvent
from speech.speech_processor import SpeechProcessor


# ============================================================================
# PROCESSOR-SIDE RIG (shape follows tests/test_speech_processor_bare_number.py)
# ============================================================================

class MockApp:
    """Mock app recording IPC calls."""

    def __init__(self):
        self.actions: List[Dict[str, Any]] = []

    async def send_command(self, payload: dict) -> bool:
        self.actions.append(payload)
        return True

    async def send_request(self, action: str, params: Optional[dict] = None,
                           timeout_s: Optional[float] = None) -> Dict[str, Any]:
        payload = {"action": action, "params": params or {}}
        self.actions.append(payload)
        return {"status": "ok"}

    def inserted_texts(self) -> List[str]:
        return [
            a["params"].get("insertion_string")
            for a in self.actions
            if a.get("action") == "intelligent_insert_text"
        ]


class MockTextParser:
    """Mock text parser that records executed command text."""

    def __init__(self):
        self.executed: List[str] = []
        self.last_executed_pattern_type: Optional[str] = "command"
        self.result: bool = True

    async def parse_and_execute(self, text, return_remainder=False,
                                authorized_command=False):
        self.executed.append(text)
        if return_remainder:
            return self.result, ""
        return self.result


class FakeOverlayMachine:
    """Stands in for ClickOverlayStateMachine: just the .state field."""

    def __init__(self, state: OverlayState):
        self.state = state


class FakeController:
    """A logic controller double with REAL attribute values.

    Deliberately not a MagicMock: the gate must read
    ``screen_read_in_flight_since`` as a plain float-or-None, and a
    MagicMock would auto-create a Mock attribute instead of None.
    """

    def __init__(self, since: Optional[float] = None,
                 overlay_state: Optional[OverlayState] = None):
        self.screen_read_in_flight_since = since
        if overlay_state is not None:
            self.click_overlay_state = FakeOverlayMachine(overlay_state)


def make_processor(logic_controller: Any = None):
    app = MockApp()
    queue = asyncio.Queue()
    catalog = MagicMock()
    catalog.command_hotword = "x-ray"
    catalog.lookup.return_value = None
    catalog.get_trailing_command.return_value = None
    text_parser = MockTextParser()

    processor = SpeechProcessor(
        word_queue=queue,
        catalog=catalog,
        text_parser=text_parser,
        app=app,
        logic_controller=logic_controller,
    )
    return processor, app, text_parser


def word(text: str, start: bool = False, uid: int = 1) -> WordEvent:
    return WordEvent(
        word=text,
        start_of_utterance=start,
        end_of_utterance=False,
        utterance_id=uid,
    )


def end_marker(uid: int = 1) -> WordEvent:
    return WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=True,
        utterance_id=uid,
        is_utterance_end_marker=True,
    )


def lifecycle_marker(uid: int = 1) -> WordEvent:
    return WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=False,
        utterance_id=uid,
        is_lifecycle_reset_marker=True,
    )


# ============================================================================
# TESTS: acceptance 1 -- dictation refused while a read is in flight
# ============================================================================

class TestReadGateRefusesDictation:

    @pytest.mark.asyncio
    async def test_dictation_word_refused_while_a_read_is_in_flight(self):
        controller = FakeController(since=time.monotonic())
        proc, app, _parser = make_processor(controller)

        await proc.process_word_event(word("hello", start=True))

        assert app.inserted_texts() == [], (
            "a dictation word sent during a screen read must be refused"
        )

    @pytest.mark.asyncio
    async def test_refused_word_never_types_after_the_read_ends(self):
        controller = FakeController(since=time.monotonic())
        proc, app, _parser = make_processor(controller)

        await proc.process_word_event(word("hello", start=True))
        # The read ends; the utterance then closes normally.
        controller.screen_read_in_flight_since = None
        await proc.process_word_event(end_marker())

        assert app.inserted_texts() == [], (
            "a refused word must be dropped, not queued for after the read"
        )

    @pytest.mark.asyncio
    async def test_dictation_resumes_after_the_read_ends(self):
        controller = FakeController(since=time.monotonic())
        proc, app, _parser = make_processor(controller)

        await proc.process_word_event(word("hello", start=True))
        controller.screen_read_in_flight_since = None
        await proc.process_word_event(word("world"))

        assert app.inserted_texts() == ["world"]

    @pytest.mark.asyncio
    async def test_gate_expires_after_its_maximum_wait(self):
        """Acceptance 4: a stuck marker cannot refuse voice forever."""
        stale = time.monotonic() - (sp_module._READ_GATE_MAX_REFUSAL_S + 1.0)
        controller = FakeController(since=stale)
        proc, app, _parser = make_processor(controller)

        await proc.process_word_event(word("hello", start=True))

        assert app.inserted_texts() == ["hello"]

    @pytest.mark.asyncio
    async def test_gate_cap_follows_the_configured_screen_read_limit(self):
        """wh-overlay-slow-uia-stale-badges.3: the cap tracks the read limit.

        The gate's fixed 10 s cap was justified by the read never being
        allowed to run that long. ``[click] screen_read_timeout_ms`` can now
        be raised to 20 s, so a fixed cap would stop refusing at 10 s while
        the Input command loop was still held by the read -- the very
        late-typed word children .7 and .11 exist to prevent. The cap is
        ``max(10.0, limit_s + 2.0)``: 22 s at a 20 s read limit.
        """
        controller = FakeController(since=time.monotonic() - 15.0)
        controller.click_config = SimpleNamespace(
            screen_read_timeout_ms=20000,
        )
        proc, app, _parser = make_processor(controller)

        await proc.process_word_event(word("hello", start=True))

        assert app.inserted_texts() == [], (
            "a 15-second-old read marker stopped refusing dictation even "
            "though [click] screen_read_timeout_ms allows a 20-second read"
        )

    @pytest.mark.asyncio
    async def test_gate_cap_still_expires_above_the_configured_limit(self):
        """The raised cap is still a cap: past it, voice comes back."""
        controller = FakeController(since=time.monotonic() - 23.0)
        controller.click_config = SimpleNamespace(
            screen_read_timeout_ms=20000,
        )
        proc, app, _parser = make_processor(controller)

        await proc.process_word_event(word("hello", start=True))

        assert app.inserted_texts() == ["hello"]

    @pytest.mark.asyncio
    async def test_gate_cap_never_drops_below_the_floor(self):
        """A read limit below the floor leaves the 10 s floor in force.

        wh-overlay-slow-uia-stale-badges.3 keeps ``_READ_GATE_MAX_REFUSAL_S``
        as the floor: a 3 s read limit must not shrink the cap to 5 s. The
        cap also covers a destroyed awaiter's stale marker, and children .7
        and .11 measured their containment against the floor, not against
        the configured limit.
        """
        controller = FakeController(since=time.monotonic() - 8.0)
        controller.click_config = SimpleNamespace(screen_read_timeout_ms=3000)
        proc, app, _parser = make_processor(controller)

        await proc.process_word_event(word("hello", start=True))

        assert app.inserted_texts() == [], (
            "an 8-second-old read marker stopped refusing dictation because "
            "a 3-second read limit pulled the cap below its 10-second floor"
        )

    @pytest.mark.asyncio
    async def test_mock_click_config_without_a_read_limit_uses_the_floor(self):
        """A Mock click_config's auto-attribute must not reach the cap maths.

        Legacy fixtures hand the processor a MagicMock controller whose
        ``click_config.screen_read_timeout_ms`` is another MagicMock. The
        gate reads that as "no limit configured" and keeps the floor; it
        must not raise TypeError from ``max`` over a Mock.
        """
        controller = FakeController(since=time.monotonic() - 8.0)
        controller.click_config = MagicMock()
        proc, app, _parser = make_processor(controller)

        await proc.process_word_event(word("hello", start=True))

        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    async def test_no_controller_dictates_unchanged(self):
        proc, app, _parser = make_processor(None)
        await proc.process_word_event(word("hello", start=True))
        assert app.inserted_texts() == ["hello"]

    @pytest.mark.asyncio
    async def test_non_numeric_marker_value_dictates_unchanged(self):
        """Legacy fixtures wire MagicMock controllers whose attribute reads
        return Mock objects; the gate must treat a non-float marker as
        no-read rather than raising."""
        proc, app, _parser = make_processor(MagicMock())
        await proc.process_word_event(word("hello", start=True))
        assert app.inserted_texts() == ["hello"]


# ============================================================================
# TESTS: acceptance 2 -- a spoken number still works during the read
# ============================================================================

class TestNumbersStillPassDuringARead:

    @pytest.mark.asyncio
    async def test_bare_number_still_clicks_during_a_refresh_read(self):
        controller = FakeController(
            since=time.monotonic(),
            overlay_state=OverlayState.REFRESH_IN_FLIGHT,
        )
        proc, app, parser = make_processor(controller)

        await proc.process_word_event(word("70", start=True))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click 70"]
        assert app.inserted_texts() == []


# ============================================================================
# TESTS: the editor path is not the Input command loop -- it stays open
# ============================================================================

class TestEditorPathPassesDuringARead:

    @pytest.mark.asyncio
    async def test_editor_routed_words_pass_during_a_read(self):
        """The gate sits AFTER editor routing: an editor insert does not
        cross the Input command loop, so refusing it protects nothing."""
        controller = MagicMock()
        controller.screen_read_in_flight_since = time.monotonic()
        controller.insert_editor_word = AsyncMock(
            side_effect=lambda text, utterance_id: len(text)
        )
        proc, app, _parser = make_processor(controller)
        # Sticky editor path: a word of this utterance already routed to
        # the editor, so this word goes straight there (no policy consult).
        proc._used_editor_this_utterance = True
        proc._current_utterance_id = 42

        await proc.process_word_event(word("hello"))

        assert controller.insert_editor_word.await_count == 1
        assert app.inserted_texts() == []


# ============================================================================
# TESTS: sentence tracking (the mid-sentence signal for acceptance 3)
# ============================================================================

def _engine(processor, app):
    """Real TextParser wired to a stub speech_handler.

    The catalog is a MagicMock: these tests hand the rule's steps to
    ``_execute_rule`` directly, so no pattern loading is involved.
    """
    handler = MagicMock()
    handler.app = app
    handler.speech_processor = processor
    return TextParser(handler, MagicMock())


class TestReplacementsRefusedDuringARead:
    """Finding wh-overlay-slow-uia-stale-badges.7.1.1: a matched
    replacement whose step types text ("period" -> ".") crosses Input's
    ONE command loop exactly like a dictated word, but the engine sends
    it directly (command_engine._execute_rule), bypassing
    _send_to_dictation. The gate must cover those sends too. Commands
    that do not type (click, press) stay unaffected."""

    def _rule(self, engine, steps):
        match = re.search(r"period", "period")
        return engine._execute_rule(match, steps, None, "replacement")

    @pytest.mark.asyncio
    async def test_a_replacement_is_refused_while_a_read_is_in_flight(self):
        controller = FakeController(since=time.monotonic())
        proc, app, _parser = make_processor(controller)
        engine = _engine(proc, app)

        consumed = await self._rule(
            engine, [{"function": "text", "params": ["."]}],
        )
        assert consumed is True, "a refused rule is consumed, not retried"
        assert app.actions == [], "the refused text must never reach Input"

    @pytest.mark.asyncio
    async def test_a_replacement_types_when_no_read_is_in_flight(self):
        controller = FakeController(since=None)
        proc, app, _parser = make_processor(controller)
        engine = _engine(proc, app)

        consumed = await self._rule(
            engine, [{"function": "text", "params": ["."]}],
        )
        assert consumed is True
        assert app.inserted_texts() == ["."]
        assert not proc.sentence_closed_event.is_set(), (
            "an engine text send opens the dictation sentence, so a "
            "read requested during a replacement-only utterance waits"
        )

    @pytest.mark.asyncio
    async def test_type_text_and_raw_insert_are_refused_too(self):
        controller = FakeController(since=time.monotonic())
        proc, app, _parser = make_processor(controller)
        engine = _engine(proc, app)

        for steps in (
            [{"function": "type_text", "params": ["find me"]}],
            [{"function": "insert_raw", "params": ["@@"]}],
        ):
            consumed = await self._rule(engine, steps)
            assert consumed is True
        assert app.actions == []

    @pytest.mark.asyncio
    async def test_an_editor_routed_replacement_passes_during_a_read(self):
        """The editor consult runs BEFORE the gate: an editor insert
        never crosses the Input loop, so the editor path keeps its
        exemption exactly as it does in _send_to_dictation."""
        controller = FakeController(since=time.monotonic())
        proc, app, _parser = make_processor(controller)
        engine = _engine(proc, app)
        proc.maybe_route_to_editor = AsyncMock(return_value=True)

        consumed = await self._rule(
            engine, [{"function": "text", "params": ["."]}],
        )
        assert consumed is True
        proc.maybe_route_to_editor.assert_awaited_once_with(".")
        assert app.actions == [], "routed to the editor, not to Input"

    @pytest.mark.asyncio
    async def test_a_refused_replacement_does_not_open_a_sentence(self):
        controller = FakeController(since=time.monotonic())
        proc, app, _parser = make_processor(controller)
        engine = _engine(proc, app)
        assert proc.sentence_closed_event.is_set()

        await self._rule(engine, [{"function": "text", "params": ["."]}])
        assert proc.sentence_closed_event.is_set(), (
            "a refused step must not clear the sentence event"
        )


class TestSentenceTracking:

    @pytest.mark.asyncio
    async def test_sentence_opens_on_first_typed_word_and_closes_at_the_end_marker(self):
        proc, app, _parser = make_processor(None)
        assert proc.sentence_closed_event.is_set(), (
            "no sentence is open before any word typed"
        )

        await proc.process_word_event(word("hello", start=True))
        assert app.inserted_texts() == ["hello"]
        assert not proc.sentence_closed_event.is_set(), (
            "a typed word opens the sentence"
        )

        await proc.process_word_event(end_marker())
        assert proc.sentence_closed_event.is_set(), (
            "the utterance-end marker closes the sentence"
        )

    @pytest.mark.asyncio
    async def test_the_lifecycle_pair_closes_the_sentence(self):
        proc, app, _parser = make_processor(None)
        await proc.process_word_event(word("hello", start=True))
        assert not proc.sentence_closed_event.is_set()

        await proc.process_word_event(lifecycle_marker())
        assert proc.sentence_closed_event.is_set()

    @pytest.mark.asyncio
    async def test_a_deferred_end_send_closes_the_sentence(self):
        proc, _app, _parser = make_processor(None)
        # The healthy shape: the deferred end belongs to the utterance
        # whose word opened the sentence.
        await proc.process_word_event(word("hello", start=True, uid=1))
        assert not proc.sentence_closed_event.is_set()
        proc._pending_utterance_end = 1

        await proc._send_pending_utterance_end()
        assert proc.sentence_closed_event.is_set()

    @pytest.mark.asyncio
    async def test_a_stale_deferred_end_does_not_close_the_live_sentence(self):
        """Finding wh-overlay-slow-uia-stale-badges.7.1.2: a deferred
        send that fires mid-way through the NEXT utterance, carrying the
        PREVIOUS utterance's id, must not close the live sentence -- a
        read would jump between two words of one sentence and get them
        refused.

        The producer this was written against is gone: the end-marker
        branch's crewcut used to leave _pending_utterance_end SET when
        that branch raised, and the per-word handler now clears it
        (wh-pending-utterance-end-stale-slot). The test injects the
        stale id directly, so it still pins the guard inside
        _send_pending_utterance_end against any future producer."""
        proc, app, _parser = make_processor(None)
        # Utterance 2 is live: its first word typed and opened the
        # sentence.
        await proc.process_word_event(word("hello", start=True, uid=2))
        assert not proc.sentence_closed_event.is_set()

        # The stale slot from utterance 1 fires its deferred send.
        proc._pending_utterance_end = 1
        await proc._send_pending_utterance_end()

        assert not proc.sentence_closed_event.is_set(), (
            "a stale deferred end must not close the live sentence"
        )
        # The stale end_utterance itself still ships. The guard here
        # covers the sentence only; refusing the send is the clipboard
        # manager's id check, and the raise path that produced this
        # shape is closed (wh-pending-utterance-end-stale-slot).
        assert {
            "action": "end_utterance", "params": {"utterance_id": 1},
        } in app.actions

    @pytest.mark.asyncio
    async def test_a_new_utterance_start_closes_the_stale_sentence(self):
        """An utterance whose end marker was lost must not hold the
        sentence open forever; the next utterance's start closes it. The
        refused word must not reopen it (refusal drops the word before
        the typing that opens a sentence)."""
        controller = FakeController(since=None)
        proc, app, _parser = make_processor(controller)

        await proc.process_word_event(word("hello", start=True, uid=1))
        assert not proc.sentence_closed_event.is_set()

        # A read starts; utterance 2 begins with no end marker for 1.
        controller.screen_read_in_flight_since = time.monotonic()
        await proc.process_word_event(word("world", start=True, uid=2))

        assert app.inserted_texts() == ["hello"], "the second word refused"
        assert proc.sentence_closed_event.is_set(), (
            "the new start closed the stale sentence; the refused word "
            "did not reopen it"
        )


# ============================================================================
# MAIN-SIDE RIG (shape follows tests/test_logic_overlay_integration.py)
# ============================================================================

class _FakeQueue:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_nowait(self, item: dict) -> None:
        self.items.append(item)


def _summary(snapshot_id: str, *display_numbers: int) -> WalkSnapshotSummary:
    items = [
        WalkSnapshotSummaryItem(
            item_id=f"{snapshot_id}-item-{n}",
            display_number=n,
            name=f"control {n}",
            role="Button",
            bounds=(0, 0, 10, 10),
            monitor_id=0,
        )
        for n in display_numbers
    ]
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id, items=items, created_at_monotonic=1.0,
    )


def _walk_response(snapshot_id, sid, gen, *display_numbers) -> dict:
    return StartOverlayWalkResponse(
        status="ok",
        outcome="ok",
        reason=None,
        snapshot_id=snapshot_id,
        snapshot_summary=_summary(snapshot_id, *display_numbers),
        trace_id="tr",
        overlay_session_id=sid,
        paint_generation=gen,
    ).to_dict()


def _controller(machine: Optional[ClickOverlayStateMachine] = None):
    """Bare LogicController with only the build-dispatch attributes."""
    controller = object.__new__(LogicController)
    controller.click_config = MagicMock()
    controller.click_config.enabled = True
    controller.click_config.overlay_enabled_effective = True
    controller.click_config.overlay_invalid_key = None
    controller.click_config.response_timeout_ms = 3000
    controller.click_config.screen_read_timeout_ms = 10000
    controller.click_overlay_state = (
        machine if machine is not None else ClickOverlayStateMachine()
    )
    controller._overlay_effect_lock = asyncio.Lock()
    controller._overlay_settle_read_identity = None
    controller._overlay_auto_open_filter = None
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = _FakeQueue()
    # Stop the build-response feed cascade: these tests assert send TIMING,
    # not machine advancement.
    controller._apply_overlay_event = MagicMock()
    return controller


def _wire_app(controller, responder):
    sent: list[tuple[str, dict]] = []

    async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
        sent.append((action, dict(params or {})))
        return responder(action, params or {})

    controller.app = MagicMock()
    controller.app.send_request = _send_request
    controller._sent = sent
    return sent


def _refresh_machine() -> ClickOverlayStateMachine:
    """Drive a machine to REFRESH_IN_FLIGHT (painted overlay, focus change)."""
    machine = ClickOverlayStateMachine()
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sid, gen = machine.overlay_session_id, machine.paint_generation
    machine.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
            paint_generation=gen, snapshot_id="snap-a",
        )
    )
    machine.apply(
        OverlayEvent(
            OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
            paint_generation=gen, paint_state=PaintAckState.PAINTED,
        )
    )
    machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    return machine


def _build_effect(machine, reason=BuildReason.REFRESH) -> Effect:
    return Effect(
        kind=EffectKind.DISPATCH_BUILD,
        overlay_session_id=machine.overlay_session_id,
        paint_generation=machine.paint_generation,
        build_reason=reason,
    )


def _wire_sentence(controller, event: asyncio.Event) -> None:
    """Attach a service_manager chain exposing ``event`` as the
    processor's sentence_closed_event."""
    processor = MagicMock()
    processor.sentence_closed_event = event
    speech_handler = MagicMock()
    speech_handler.speech_processor = processor
    service_manager = MagicMock()
    service_manager.speech_handler = speech_handler
    controller.service_manager = service_manager


# ============================================================================
# TESTS: acceptance 3 -- a read requested mid-sentence waits
# ============================================================================

class TestReadWaitsForTheSentenceEnd:

    def test_read_requested_mid_sentence_waits_for_the_sentence_end(self):
        async def _run():
            machine = _refresh_machine()
            sid, gen = machine.overlay_session_id, machine.paint_generation
            controller = _controller(machine)
            controller.loop = asyncio.get_running_loop()
            sentence_closed = asyncio.Event()  # cleared = sentence open
            _wire_sentence(controller, sentence_closed)
            _wire_app(
                controller,
                lambda a, p: _walk_response("snap-b", sid, gen, 1),
            )

            task = asyncio.get_running_loop().create_task(
                controller._dispatch_overlay_effects(
                    (_build_effect(machine),), trace_id="tr",
                )
            )
            for _ in range(10):
                await asyncio.sleep(0)
            walks_before = [a for a, _ in controller._sent
                            if a == "start_overlay_walk"]

            sentence_closed.set()
            await asyncio.wait_for(task, timeout=2.0)
            walks_after = [a for a, _ in controller._sent
                           if a == "start_overlay_walk"]
            return walks_before, walks_after

        walks_before, walks_after = asyncio.run(_run())
        assert walks_before == [], (
            "the read must not reach Input while the sentence is open"
        )
        assert walks_after == ["start_overlay_walk"], (
            "the read goes out once the sentence closes"
        )

    def test_the_sentence_wait_gives_up_at_its_bound(self):
        async def _run():
            machine = _refresh_machine()
            sid, gen = machine.overlay_session_id, machine.paint_generation
            controller = _controller(machine)
            controller.loop = asyncio.get_running_loop()
            controller._overlay_read_sentence_wait_max_s = 0.05
            sentence_closed = asyncio.Event()  # never set
            _wire_sentence(controller, sentence_closed)
            _wire_app(
                controller,
                lambda a, p: _walk_response("snap-b", sid, gen, 1),
            )

            await asyncio.wait_for(
                controller._dispatch_overlay_effects(
                    (_build_effect(machine),), trace_id="tr",
                ),
                timeout=2.0,
            )
            return [a for a, _ in controller._sent
                    if a == "start_overlay_walk"]

        assert asyncio.run(_run()) == ["start_overlay_walk"], (
            "a sentence that never closes must not block the read past "
            "the wait bound"
        )

    def test_a_hide_during_the_sentence_wait_wakes_the_build(self):
        async def _run():
            machine = _refresh_machine()
            controller = _controller(machine)
            controller.loop = asyncio.get_running_loop()
            sentence_closed = asyncio.Event()  # sentence stays open
            _wire_sentence(controller, sentence_closed)
            _wire_app(controller, lambda a, p: {"status": "ok"})

            task = asyncio.get_running_loop().create_task(
                controller._dispatch_overlay_effects(
                    (_build_effect(machine),), trace_id="tr",
                )
            )
            for _ in range(10):
                await asyncio.sleep(0)

            # The user says "hide numbers": the machine closes and the
            # committer wakes any parked build.
            machine.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
            controller._abort_inflight_overlay_build()
            await asyncio.wait_for(task, timeout=1.0)
            return [a for a, _ in controller._sent
                    if a == "start_overlay_walk"]

        assert asyncio.run(_run()) == [], (
            "a hide during the sentence wait must wake the build promptly "
            "and skip the stale send"
        )

    def test_a_build_queued_past_a_hide_skips_the_sentence_wait(self):
        """Finding wh-overlay-slow-uia-stale-badges.7.1.4: a hide can
        commit while this build batch is still QUEUED behind
        _overlay_effect_lock. The committer's single
        _abort_inflight_overlay_build call runs BEFORE the build parks
        its abort, so that wake is lost for good -- the wait must
        notice the machine has left the build behind and return at
        once, not sleep out its 5 s bound holding the lock while the
        hide's clear batch queues behind it."""
        async def _run():
            machine = _refresh_machine()
            controller = _controller(machine)
            controller.loop = asyncio.get_running_loop()
            sentence_closed = asyncio.Event()  # sentence stays open
            _wire_sentence(controller, sentence_closed)
            _wire_app(controller, lambda a, p: {"status": "ok"})
            effect = _build_effect(machine)

            # The hide commits BEFORE the build batch runs; its abort
            # call finds nothing parked (the lost wake).
            machine.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
            controller._abort_inflight_overlay_build()

            # Far below the default 5 s wait bound: a lost wake that
            # sleeps out the bound trips this timeout.
            await asyncio.wait_for(
                controller._dispatch_overlay_effects(
                    (effect,), trace_id="tr",
                ),
                timeout=0.8,
            )
            return [a for a, _ in controller._sent
                    if a == "start_overlay_walk"]

        assert asyncio.run(_run()) == [], "the stale build must skip the send"

    def test_a_build_queued_past_a_supersede_skips_the_sentence_wait(self):
        """The pair-mismatch half of the same lost-wake defect: the
        machine moved on to a NEW session while the old build batch was
        still queued."""
        async def _run():
            machine = _refresh_machine()
            sid, gen = machine.overlay_session_id, machine.paint_generation
            controller = _controller(machine)
            controller.loop = asyncio.get_running_loop()
            sentence_closed = asyncio.Event()  # sentence stays open
            _wire_sentence(controller, sentence_closed)
            _wire_app(controller, lambda a, p: {"status": "ok"})
            effect = _build_effect(machine)

            machine.apply(OverlayEvent(OverlayEventKind.HIDE_NUMBERS))
            machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
            assert (
                machine.overlay_session_id, machine.paint_generation,
            ) != (sid, gen), "test rig: the machine must have moved on"
            assert machine.state is not OverlayState.CLOSED
            controller._abort_inflight_overlay_build()

            await asyncio.wait_for(
                controller._dispatch_overlay_effects(
                    (effect,), trace_id="tr",
                ),
                timeout=0.8,
            )
            return [a for a, _ in controller._sent
                    if a == "start_overlay_walk"]

        assert asyncio.run(_run()) == [], "the stale build must skip the send"


# ============================================================================
# TESTS: the read-in-flight marker spans exactly the Input round trip
# ============================================================================

class TestReadMarkerLifecycle:

    def test_marker_set_during_the_round_trip_and_cleared_after(self):
        async def _run():
            machine = _refresh_machine()
            sid, gen = machine.overlay_session_id, machine.paint_generation
            controller = _controller(machine)
            controller.loop = asyncio.get_running_loop()
            seen: list = []

            async def _send_request(action, params=None, timeout_s=None,
                                    on_late_response=None):
                seen.append(controller.screen_read_in_flight_since)
                return _walk_response("snap-b", sid, gen, 1)

            controller.app = MagicMock()
            controller.app.send_request = _send_request
            controller._sent = []

            await asyncio.wait_for(
                controller._dispatch_overlay_effects(
                    (_build_effect(machine),), trace_id="tr",
                ),
                timeout=2.0,
            )
            return seen, controller.screen_read_in_flight_since

        seen, after = asyncio.run(_run())
        assert len(seen) == 1
        assert isinstance(seen[0], float), (
            "the marker must be a monotonic stamp while Input runs the read"
        )
        assert after is None, "the marker must clear when the read ends"

    def test_marker_cleared_when_the_send_times_out(self):
        async def _run():
            machine = _refresh_machine()
            controller = _controller(machine)
            controller.loop = asyncio.get_running_loop()

            async def _send_request(action, params=None, timeout_s=None,
                                    on_late_response=None):
                raise asyncio.TimeoutError()

            controller.app = MagicMock()
            controller.app.send_request = _send_request
            controller._sent = []

            await asyncio.wait_for(
                controller._dispatch_overlay_effects(
                    (_build_effect(machine),), trace_id="tr",
                ),
                timeout=2.0,
            )
            return controller.screen_read_in_flight_since

        assert asyncio.run(_run()) is None, (
            "a timed-out read must not leave the marker set"
        )


# ============================================================================
# TESTS: the by-name click walk marks the round trip too
# (rig shape follows tests/test_click_flow.py)
# ============================================================================

def _click_controller(*, send_result=None, send_exc=None):
    """MagicMock(spec=LogicController) with forward_click_element bound."""
    controller = MagicMock(spec=LogicController)
    controller.forward_click_element = (
        LogicController.forward_click_element.__get__(controller)
    )
    controller._forward_click_notice = (
        LogicController._forward_click_notice.__get__(controller)
    )
    controller.click_config = ClickConfig.from_raw(
        {"enabled": True, "response_timeout_ms": 3000}
    )
    controller._click_disabled_notice_shown = False
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    controller.screen_read_in_flight_since = None
    # Bind the real marker helpers when they exist: the spec'd mock
    # would otherwise swallow the site's helper calls. The getattr
    # keeps the rig importable while the helpers are red.
    for _name in ("_mark_screen_read_started", "_mark_screen_read_finished"):
        _real = getattr(LogicController, _name, None)
        if _real is not None:
            setattr(controller, _name, _real.__get__(controller))
    seen: list = []

    async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
        seen.append(controller.screen_read_in_flight_since)
        if send_exc is not None:
            raise send_exc
        return send_result

    controller.app = MagicMock()
    controller.app.send_request = _send_request
    controller._seen_markers = seen
    return controller


class TestByNameClickMarksTheRoundTrip:

    def test_click_element_marker_spans_the_send(self):
        # A malformed ({}) reply exercises the post-send handling without
        # a full ClickElementResponse; the marker must still clear.
        controller = _click_controller(send_result={})
        query = ElementQuery("cancel", "Button", None, None, "cancel")
        asyncio.run(controller.forward_click_element(query, "tr-click"))

        assert len(controller._seen_markers) == 1
        assert isinstance(controller._seen_markers[0], float), (
            "the by-name walk must mark the round trip for the "
            "dictation gate"
        )
        assert controller.screen_read_in_flight_since is None

    def test_click_element_marker_cleared_on_timeout(self):
        controller = _click_controller(send_exc=asyncio.TimeoutError())
        query = ElementQuery("cancel", "Button", None, None, "cancel")
        asyncio.run(controller.forward_click_element(query, "tr-click"))

        assert controller.screen_read_in_flight_since is None, (
            "a timed-out by-name walk must not leave the marker set"
        )


# ============================================================================
# TESTS: overlapping walks share one marker
# (finding wh-overlay-slow-uia-stale-badges.7.1.3)
# ============================================================================

class TestOverlappingReadsKeepTheMarker:
    """The build walk and the by-name click walk can overlap
    (forward_click_element takes no lock). With a single Optional slot
    the FIRST finisher cleared the stamp while the second walk still
    blocked Input's command loop, so the gate admitted words that
    queued behind the remaining walk."""

    def test_the_first_finisher_keeps_the_marker_until_the_last(self):
        controller = _controller()
        controller.screen_read_in_flight_since = None

        controller._mark_screen_read_started()   # walk A
        controller._mark_screen_read_started()   # walk B overlaps
        controller._mark_screen_read_finished()  # A ends first
        assert isinstance(controller.screen_read_in_flight_since, float), (
            "one walk finished but another still runs; the marker stays"
        )
        controller._mark_screen_read_finished()  # B ends
        assert controller.screen_read_in_flight_since is None

    def test_the_build_walk_counts_its_round_trip(self):
        """The SITE must use the shared count, not a raw stamp write."""
        async def _run():
            machine = _refresh_machine()
            sid, gen = machine.overlay_session_id, machine.paint_generation
            controller = _controller(machine)
            controller.loop = asyncio.get_running_loop()
            counts: list = []

            async def _send_request(action, params=None, timeout_s=None,
                                    on_late_response=None):
                # getattr, not direct access: a mutant that skips the
                # helper must fail the counts assert below, not crash
                # here on the missing attribute.
                counts.append(
                    getattr(controller, "_screen_reads_in_flight", 0)
                )
                return _walk_response("snap-b", sid, gen, 1)

            controller.app = MagicMock()
            controller.app.send_request = _send_request
            controller._sent = []

            await asyncio.wait_for(
                controller._dispatch_overlay_effects(
                    (_build_effect(machine),), trace_id="tr",
                ),
                timeout=2.0,
            )
            return counts, controller._screen_reads_in_flight

        counts, after = asyncio.run(_run())
        assert counts == [1]
        assert after == 0

    def test_a_click_walk_finishing_keeps_a_build_walks_marker(self):
        controller = _click_controller(send_result={})
        # A build walk is already in flight when the click walk runs.
        controller._mark_screen_read_started()

        query = ElementQuery("cancel", "Button", None, None, "cancel")
        asyncio.run(controller.forward_click_element(query, "tr-click"))

        assert isinstance(controller.screen_read_in_flight_since, float), (
            "the click's finally must not clear the build walk's marker"
        )
        controller._mark_screen_read_finished()
        assert controller.screen_read_in_flight_since is None


# ============================================================================
# TESTS: wrap / transform / go-fallback coverage and failed-send restore
# (codex round-2 findings wh-overlay-slow-uia-stale-badges.7.1.5 - .7)
# ============================================================================

class TestWrapAndTransformRefusedDuringARead:
    """Finding wh-overlay-slow-uia-stale-badges.7.1.6: wrap_or_insert and
    transform_selection payloads mutate foreground text through Input's
    ONE command loop, but the engine's text-action set omitted them. The
    scope forms also run an awaited selection hotkey FIRST, so the whole
    rule must be refused before its first Input-side step."""

    def _rule(self, engine, steps):
        match = re.search(r"period", "period")
        return engine._execute_rule(match, steps, None, "command")

    @pytest.mark.asyncio
    async def test_a_wrap_command_is_refused_while_a_read_is_in_flight(self):
        controller = FakeController(since=time.monotonic())
        proc, app, _parser = make_processor(controller)
        engine = _engine(proc, app)

        consumed = await self._rule(
            engine,
            [{"function": "wrap_or_insert", "params": ["(", ")", " hello"]}],
        )
        assert consumed is True, "a refused rule is consumed, not retried"
        assert app.actions == [], "the wrap must never reach Input"

    @pytest.mark.asyncio
    async def test_a_direct_transform_is_refused_while_a_read_is_in_flight(self):
        controller = FakeController(since=time.monotonic())
        proc, app, _parser = make_processor(controller)
        engine = _engine(proc, app)

        consumed = await self._rule(
            engine,
            [{"function": "transform_selection", "params": ["uppercase"],
              "awaits_done": True}],
        )
        assert consumed is True
        assert app.actions == [], "the transform must never reach Input"

    @pytest.mark.asyncio
    async def test_a_scope_transform_is_refused_before_its_selection_hotkey(self):
        """The scope forms ("uppercase next two words") run an awaited hk
        selection step before the transform. Refusing only at the
        transform's own dispatch would first queue the hk behind the
        read; the whole rule must be refused up front."""
        controller = FakeController(since=time.monotonic())
        proc, app, _parser = make_processor(controller)
        engine = _engine(proc, app)

        consumed = await self._rule(
            engine,
            [
                {"function": "hk",
                 "params": ["shift", "ctrl", "right"],
                 "awaits_done": True},
                {"function": "transform_selection", "params": ["uppercase"],
                 "awaits_done": True},
            ],
        )
        assert consumed is True
        assert app.actions == [], (
            "neither the selection hotkey nor the transform may reach "
            "Input while a read is in flight"
        )

    @pytest.mark.asyncio
    async def test_a_wrap_types_when_no_read_is_in_flight(self):
        controller = FakeController(since=None)
        proc, app, _parser = make_processor(controller)
        engine = _engine(proc, app)

        consumed = await self._rule(
            engine,
            [{"function": "wrap_or_insert", "params": ["(", ")", " hello"]}],
        )
        assert consumed is True
        assert [a.get("action") for a in app.actions] == ["wrap_or_insert"]
        assert not proc.sentence_closed_event.is_set(), (
            "a wrap mutates foreground text, so it opens the dictation "
            "sentence exactly as an engine text send does"
        )


class TestGoFallbackGatedDuringARead:
    """Finding wh-overlay-slow-uia-stale-badges.7.1.5: the
    cursor_navigate dictation fallback sent an intelligent_insert_text
    payload directly through app.send_command, bypassing the screen-read
    gate, the editor consult, and sentence tracking."""

    def _actions(self, proc, app):
        from speech.actions import ActionFunctions
        handler = MagicMock()
        handler.app = app
        handler.speech_processor = proc
        return ActionFunctions(handler)

    @pytest.mark.asyncio
    async def test_an_unparseable_go_phrase_is_refused_while_a_read_is_in_flight(self):
        controller = FakeController(since=time.monotonic())
        proc, app, _parser = make_processor(controller)
        af = self._actions(proc, app)

        await af.cursor_navigate("go blurble frotz")
        assert app.actions == [], (
            "the dictation fallback must consult the gate; nothing may "
            "reach Input while a read is in flight"
        )

    @pytest.mark.asyncio
    async def test_an_unparseable_go_phrase_dictates_through_the_helper(self):
        controller = FakeController(since=None)
        proc, app, parser = make_processor(controller)
        af = self._actions(proc, app)

        await af.cursor_navigate("go blurble frotz")
        assert app.inserted_texts() == ["go blurble frotz"], (
            "with no read in flight the fallback still types the phrase"
        )
        assert not proc.sentence_closed_event.is_set(), (
            "the fallback goes through _send_to_dictation, which opens "
            "the sentence"
        )
        assert getattr(parser, "dictation_fallback_this_parse", False) is True, (
            "the fallback marks the parse as dictation so a later STT "
            "revision can still retract the typed words"
        )


class FalseSendApp(MockApp):
    """send_command reports the payload was NOT accepted (queue full)."""

    async def send_command(self, payload: dict) -> bool:
        await super().send_command(payload)
        return False


class RaisingApp(MockApp):
    """send_request raises the given exception before enqueuing."""

    def __init__(self, exc: BaseException):
        super().__init__()
        self._exc = exc

    async def send_request(self, action: str, params: Optional[dict] = None,
                           timeout_s: Optional[float] = None):
        raise self._exc


class TestFailedSendsRestoreTheSentence:
    """Finding wh-overlay-slow-uia-stale-badges.7.1.7: the sentence event
    is cleared before the send, but a payload the app PROVABLY never
    accepted (send_command returning False, or the pre-enqueue
    IpcDeliveryError from send_request) leaves no Input work that could
    ever produce an end_utterance -- the event must be restored. A
    timeout is different: durable dictation can still deliver late, so
    the event stays conservatively cleared."""

    def _rule(self, engine, steps):
        match = re.search(r"period", "period")
        return engine._execute_rule(match, steps, None, "replacement")

    @pytest.mark.asyncio
    async def test_a_failed_fire_and_forget_send_restores_the_closed_sentence(self):
        controller = FakeController(since=None)
        proc, _app, _parser = make_processor(controller)
        app = FalseSendApp()
        engine = _engine(proc, app)
        assert proc.sentence_closed_event.is_set()

        consumed = await self._rule(
            engine, [{"function": "text", "params": ["."]}],
        )
        assert consumed is True
        assert proc.sentence_closed_event.is_set(), (
            "a send the app refused (False) never enqueued anything; the "
            "sentence it opened must be closed again"
        )

    @pytest.mark.asyncio
    async def test_a_failed_send_keeps_a_sentence_an_earlier_word_opened(self):
        controller = FakeController(since=None)
        proc, _app, _parser = make_processor(controller)
        app = FalseSendApp()
        engine = _engine(proc, app)
        # An earlier accepted word already opened the sentence.
        proc.sentence_closed_event.clear()

        await self._rule(engine, [{"function": "text", "params": ["."]}])
        assert not proc.sentence_closed_event.is_set(), (
            "a failed step must not close a sentence that was already "
            "open because of an earlier accepted word"
        )

    @pytest.mark.asyncio
    async def test_a_queue_full_dictation_send_restores_the_closed_sentence(self):
        from services.wheelhouse.app import IpcDeliveryError
        controller = FakeController(since=None)
        proc, _app, _parser = make_processor(controller)
        proc.app = RaisingApp(IpcDeliveryError("outbound IPC queue full"))
        assert proc.sentence_closed_event.is_set()

        with pytest.raises(IpcDeliveryError):
            await proc._send_to_dictation("hello")
        assert proc.sentence_closed_event.is_set(), (
            "the queue-full failure happens BEFORE the payload enters "
            "the outbound queue; no Input work exists, so the sentence "
            "must be closed again"
        )

    @pytest.mark.asyncio
    async def test_a_timeout_keeps_the_sentence_conservatively_open(self):
        controller = FakeController(since=None)
        proc, _app, _parser = make_processor(controller)
        proc.app = RaisingApp(asyncio.TimeoutError())
        assert proc.sentence_closed_event.is_set()

        with pytest.raises(asyncio.TimeoutError):
            await proc._send_to_dictation("hello")
        assert not proc.sentence_closed_event.is_set(), (
            "a timed-out durable request may still deliver late and "
            "type; the sentence stays open so a read keeps waiting"
        )

    @pytest.mark.asyncio
    async def test_an_awaited_engine_send_queue_full_restores_the_closed_sentence(self):
        from services.wheelhouse.app import IpcDeliveryError
        controller = FakeController(since=None)
        proc, _app, _parser = make_processor(controller)
        app = RaisingApp(IpcDeliveryError("outbound IPC queue full"))
        engine = _engine(proc, app)
        assert proc.sentence_closed_event.is_set()

        consumed = await self._rule(
            engine,
            [{"function": "text", "params": ["."], "awaits_done": True}],
        )
        assert consumed is False, (
            "the delivery error abandons the rule (existing behavior)"
        )
        assert proc.sentence_closed_event.is_set(), (
            "the pre-enqueue failure restores the sentence the step had "
            "opened"
        )


class CaptureApp(MockApp):
    """send_request answers the AI transform's two Input requests."""

    def __init__(self, captured_text: str = "teh quick"):
        super().__init__()
        self._captured_text = captured_text

    async def send_request(self, action: str, params: Optional[dict] = None,
                           timeout_s: Optional[float] = None) -> Dict[str, Any]:
        payload = {"action": action, "params": params or {}}
        self.actions.append(payload)
        if action == "capture_selected_text":
            return {
                "text": self._captured_text,
                "capture_token": 3,
                "target_hwnd": 42,
            }
        return {"success": True}


class TestAiTransformsGatedDuringARead:
    """Finding wh-overlay-slow-uia-stale-badges.7.1.8: fix_text_ai and
    rewrite_text_ai mutate foreground text (capture the selection, run
    the model, paste the answer) but bypassed both gate layers: the
    whole-rule pre-scan did not classify them, and
    _run_ai_text_transform sent replace_selected_text without consulting
    the gate, so a read that began during the model await still got the
    paste queued behind it in Input's one command loop."""

    def _rule(self, engine, steps):
        match = re.search(r"fix", "fix")
        return engine._execute_rule(match, steps, None, "command")

    def _transform_rig(self, proc, app, controller,
                       mark_read_during_model=False):
        from speech.actions import ActionFunctions
        handler = MagicMock()
        handler.app = app
        handler.speech_processor = proc
        af = ActionFunctions(handler)

        ai = MagicMock()
        ai.is_ready = MagicMock(return_value=True)
        ai.is_processing = MagicMock(return_value=False)
        ai._processing_lock = asyncio.Lock()
        ai.speak = AsyncMock()
        ai.speak_brief = AsyncMock()
        ai.cancel_requested = False
        af._get_ai_service = lambda: ai

        async def fake_model(_ai, text):
            if mark_read_during_model:
                controller.screen_read_in_flight_since = time.monotonic()
            return SimpleNamespace(status=None, ok=True, text=text.upper())

        return af, ai, fake_model

    async def _transform(self, af, fake_model):
        return await af._run_ai_text_transform(
            send=fake_model,
            working_word="Correcting",
            no_text_message="No text to correct.",
            failed_message="Correction failed. Original text preserved.",
        )

    @pytest.mark.asyncio
    async def test_an_ai_transform_rule_is_refused_before_its_capture(self):
        controller = FakeController(since=time.monotonic())
        proc, app, _parser = make_processor(controller)
        engine = _engine(proc, app)

        for steps in (
            [{"function": "fix_text_ai"}],
            [{"function": "rewrite_text_ai", "params": ["Rewrite plainly."]}],
        ):
            consumed = await self._rule(engine, steps)
            assert consumed is True, "a refused rule is consumed, not retried"
        assert app.actions == [], (
            "a refused AI transform must never send capture_selected_text; "
            "the capture would queue behind the in-flight read"
        )

    @pytest.mark.asyncio
    async def test_a_read_during_the_model_await_suppresses_the_paste(self):
        controller = FakeController(since=None)
        proc, _mock_app, _parser = make_processor(controller)
        app = CaptureApp()
        af, ai, fake_model = self._transform_rig(
            proc, app, controller, mark_read_during_model=True,
        )

        await self._transform(af, fake_model)
        sent = [a["action"] for a in app.actions]
        assert "capture_selected_text" in sent, (
            "no read was in flight at the start, so the capture runs"
        )
        assert "replace_selected_text" not in sent, (
            "a read that began during the model await must suppress the "
            "paste; sent to Input it would queue behind the read"
        )
        from tests.test_ai.test_silent_actions import notifications
        assert any("screen read was in progress" in message for message in notifications(af))
        ai.speak.assert_not_called()
        ai.speak_brief.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_paste_still_lands_when_no_read_is_in_flight(self):
        controller = FakeController(since=None)
        proc, _mock_app, _parser = make_processor(controller)
        app = CaptureApp()
        af, ai, fake_model = self._transform_rig(
            proc, app, controller, mark_read_during_model=False,
        )

        await self._transform(af, fake_model)
        sent = [a["action"] for a in app.actions]
        assert sent == ["capture_selected_text", "replace_selected_text"], (
            "with no read in flight the transform pastes normally"
        )
        from tests.test_ai.test_silent_actions import notifications
        assert "Done." in notifications(af)
        ai.speak.assert_not_called()
        ai.speak_brief.assert_not_called()


class TestAutoOpenRepaintIsNotGated:
    """Finding wh-overlay-slow-uia-stale-badges.7.1.9: the AUTO_OPEN
    build re-paints a snapshot Input already holds
    (show_numbered_overlay) -- no UIA walk runs -- but
    _overlay_dispatch_build applied the sentence wait and the read
    marker to every build. The auto-open is scheduled after an
    ambiguous by-name click, so it can overlap the next utterance:
    the misapplied marker refused ordinary dictation for the whole
    repaint round trip, and the misapplied wait could hold the
    repaint for the 5 s sentence bound."""

    def test_an_auto_open_repaint_does_not_set_the_marker(self):
        async def _run():
            machine = _refresh_machine()
            controller = _controller(machine)
            controller.loop = asyncio.get_running_loop()
            controller.screen_read_in_flight_since = None
            seen: list = []

            async def _send_request(action, params=None, timeout_s=None,
                                    on_late_response=None):
                seen.append(
                    (action, controller.screen_read_in_flight_since)
                )
                return {"status": "ok"}

            controller.app = MagicMock()
            controller.app.send_request = _send_request
            controller._sent = []

            await asyncio.wait_for(
                controller._dispatch_overlay_effects(
                    (_build_effect(machine, reason=BuildReason.AUTO_OPEN),),
                    trace_id="tr",
                ),
                timeout=2.0,
            )
            return seen, controller.screen_read_in_flight_since

        seen, after = asyncio.run(_run())
        assert seen == [("show_numbered_overlay", None)], (
            "a snapshot repaint runs no UIA walk; it must not set the "
            "marker that refuses dictation"
        )
        assert after is None

    def test_an_auto_open_repaint_skips_the_sentence_wait(self):
        async def _run():
            machine = _refresh_machine()
            controller = _controller(machine)
            controller.loop = asyncio.get_running_loop()
            sentence_closed = asyncio.Event()  # sentence stays open
            _wire_sentence(controller, sentence_closed)
            _wire_app(controller, lambda a, p: {"status": "ok"})

            # Far below the 5 s sentence bound: a repaint parked on the
            # sentence wait trips this timeout.
            await asyncio.wait_for(
                controller._dispatch_overlay_effects(
                    (_build_effect(machine, reason=BuildReason.AUTO_OPEN),),
                    trace_id="tr",
                ),
                timeout=0.8,
            )
            return [a for a, _ in controller._sent]

        assert asyncio.run(_run()) == ["show_numbered_overlay"], (
            "the snapshot repaint must not wait for the open sentence"
        )


class RaisingCommandApp(MockApp):
    """send_command raises the given exception before enqueuing."""

    def __init__(self, exc: BaseException):
        super().__init__()
        self._exc = exc

    async def send_command(self, payload: dict) -> bool:
        raise self._exc


class TestSerializationFailureRestoresTheSentence:
    """Finding wh-overlay-slow-uia-stale-badges.7.1.10: both send
    helpers serialize and size-check the payload BEFORE enqueuing
    (app._serialize_final_payload), and the oversize rejection raised a
    plain ValueError -- a proven never-enqueued failure that none of
    the three sentence restore sites recognized. The rejection is now
    IpcSerializationError, a subclass of BOTH IpcDeliveryError (so the
    restore sites treat it as never-sent) and ValueError (so the
    documented oversize contract survives)."""

    def _rule(self, engine, steps):
        match = re.search(r"period", "period")
        return engine._execute_rule(match, steps, None, "replacement")

    def test_the_oversize_rejection_is_a_never_enqueued_failure(self):
        from services.wheelhouse.app import IpcDeliveryError, IpcSerializationError, WheelHouseApp
        fake = SimpleNamespace(shm=SimpleNamespace(size=64))
        big = {
            "action": "intelligent_insert_text",
            "params": {"insertion_string": "y" * 4096},
        }
        with pytest.raises(
            IpcSerializationError, match="exceeds shared memory capacity",
        ) as excinfo:
            WheelHouseApp._serialize_final_payload(fake, big)
        assert isinstance(excinfo.value, ValueError), (
            "the documented oversize ValueError contract must survive"
        )
        assert isinstance(excinfo.value, IpcDeliveryError), (
            "the rejection happens before any enqueue, so the restore "
            "sites must see it as a delivery failure"
        )

    @pytest.mark.asyncio
    async def test_an_oversize_dictation_send_restores_the_closed_sentence(self):
        from services.wheelhouse.app import IpcSerializationError
        controller = FakeController(since=None)
        proc, _app, _parser = make_processor(controller)
        proc.app = RaisingApp(IpcSerializationError("payload too large"))
        assert proc.sentence_closed_event.is_set()

        with pytest.raises(IpcSerializationError):
            await proc._send_to_dictation("hello")
        assert proc.sentence_closed_event.is_set(), (
            "the oversize rejection happens before the payload enters "
            "the outbound queue; the sentence must be closed again"
        )

    @pytest.mark.asyncio
    async def test_an_oversize_awaited_engine_send_restores_the_closed_sentence(self):
        from services.wheelhouse.app import IpcSerializationError
        controller = FakeController(since=None)
        proc, _app, _parser = make_processor(controller)
        app = RaisingApp(IpcSerializationError("payload too large"))
        engine = _engine(proc, app)
        assert proc.sentence_closed_event.is_set()

        consumed = await self._rule(
            engine,
            [{"function": "text", "params": ["."], "awaits_done": True}],
        )
        assert consumed is False, (
            "the delivery failure abandons the rule (existing behavior)"
        )
        assert proc.sentence_closed_event.is_set(), (
            "the pre-enqueue rejection restores the sentence the step "
            "had opened"
        )

    @pytest.mark.asyncio
    async def test_an_oversize_fire_and_forget_send_restores_the_closed_sentence(self):
        from services.wheelhouse.app import IpcSerializationError
        controller = FakeController(since=None)
        proc, _app, _parser = make_processor(controller)
        app = RaisingCommandApp(IpcSerializationError("payload too large"))
        engine = _engine(proc, app)
        assert proc.sentence_closed_event.is_set()

        consumed = await self._rule(
            engine, [{"function": "text", "params": ["."]}],
        )
        assert consumed is False, (
            "the raised delivery failure abandons the rule"
        )
        assert proc.sentence_closed_event.is_set(), (
            "the serialization rejection never enqueued anything; the "
            "sentence the step opened must be closed again"
        )
