"""Mode 1 SpeechProcessor + fake-app integration test for the retraction policy.

wh-sqa8: Test 1 from the wh-zu7w3 plan -- the piece intentionally left out of
test_retraction_policy.py, whose tests stop at the WebSocketManager word-queue
boundary. This test wires a REAL WebSocketManager and a REAL SpeechProcessor
to one fake WheelHouseApp that records send_command and send_request into a
SINGLE ordered call list, then drives the UTT-67 Mode 1 trace (stable phrase,
no eos, disagreeing fallback final with final_reason=GOOGLE_SILENCE_2S).

The assertion is the exact IPC stream the Input process would observe:

    start_utterance          (WebSocketManager, first stable delta)
    5 phrase-1 inserts       (SpeechProcessor: this/should/not/be/P1)
    end_utterance            (lifecycle reset marker, phrase 1 closes)
    start_utterance          (lifecycle reset marker, phrase 2 opens)
    9 phrase-2 inserts       (and/it/should/be/one/of/the/children/of)
    end_utterance            (utterance end marker)
    end_utterance            (disconnect cleanup marker -- see below)

The CRITICAL property (wh-58vf.5 ordering hazard): the second end_utterance
and second start_utterance both come AFTER all five phrase-1 inserts, because
the lifecycle reset marker rides the same word_queue as the phrase-1 words.
If the marker's IPC pair were sent directly from the WebSocketManager instead
of through the queue, phrase 1 would close before its words finished
inserting and phrase 2's words would land in a dead utterance scope.

The trailing third end_utterance is the deterministic disconnect cleanup:
handle_connection's remove_client sees UTT-67 still current when the fake
websocket ends and queues a cleanup end marker (idempotent on the Input side).

No retraction IPC may appear anywhere in the stream.
"""
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

# Add parent directories to path for imports
project_root = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from speech.speech_processor import SpeechProcessor

from test_retraction_policy import (
    FakeWebsocket,
    final_message,
    make_manager,
    stable_message,
)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def manager(event_loop):
    return make_manager(event_loop)


class OrderedRecordingApp:
    """Fake WheelHouseApp recording every IPC call into one ordered list.

    Both APIs land in the same list so the test can assert cross-API
    ordering (lifecycle commands vs dictation requests), which per-API
    mock call lists cannot express.
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []

    async def send_command(self, payload: dict) -> None:
        self.calls.append(("command", payload["action"], payload.get("params", {})))

    async def send_request(
        self,
        action: str,
        params: Optional[dict] = None,
        timeout_s: Optional[float] = None,
    ) -> dict:
        self.calls.append(("request", action, params or {}))
        # _send_to_dictation awaits the response; a non-success shape would
        # block the pipeline the same way a dead Input process would.
        return {"success": True}


class NoOpTextParser:
    """Text parser that never matches commands (all text goes to dictation)."""

    async def parse_and_execute(self, text, return_remainder=False):
        if return_remainder:
            return False, text
        return False


def make_processor(word_queue: asyncio.Queue, app: OrderedRecordingApp) -> SpeechProcessor:
    """Real SpeechProcessor over the manager's word_queue, dictation-only.

    The catalog mock returns None from lookup/get_trailing_command so every
    word takes the immediate-DICTATE path -- one intelligent_insert_text per
    word, which is what makes the ordering assertion exact.
    """
    catalog = MagicMock()
    catalog.command_hotword = "x-ray"
    catalog.lookup.return_value = None
    catalog.get_trailing_command.return_value = None

    processor = SpeechProcessor(
        word_queue=word_queue,
        catalog=catalog,
        text_parser=NoOpTextParser(),
        app=app,
    )
    processor.context_mirror = MagicMock()
    processor.context_mirror.read_context.return_value = {
        "app_name": "test.exe",
        "window_title": "Test",
    }
    return processor


async def drain_processor(processor: SpeechProcessor, timeout_s: float = 5.0) -> None:
    """Wait until the consumer has emptied the queue and settled."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while not processor.word_queue.empty():
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("word_queue did not drain")
        await asyncio.sleep(0.01)
    # empty() flips as soon as the LAST event is dequeued; give the loop a
    # beat to finish processing it (all awaits are on the instant fake app).
    await asyncio.sleep(0.05)


class TestMode1IpcOrdering:
    """UTT-67 end-to-end: websocket messages in, ordered IPC stream out."""

    @pytest.mark.asyncio
    async def test_mode1_two_phrases_no_eos(self, manager):
        utt = 67
        app = OrderedRecordingApp()
        # One fake app on BOTH sides of the queue: the manager sends the
        # initial start_utterance directly; the processor sends everything
        # else while consuming the queue.
        manager._app = app
        processor = make_processor(manager.word_queue, app)

        ws = FakeWebsocket([
            # Real UTT-67 shape: Google re-sends an identical stable during
            # the pause; the repeat is a no-op delta and must not flip the
            # stable-disagreement flag (which would misroute the final to
            # Mode 3 retract+replay instead of Mode 1).
            stable_message("this should not be P1", utt),
            stable_message("this should not be P1", utt),
            final_message(
                "and it should be one of the children of",
                utt,
                final_reason="GOOGLE_SILENCE_2S",
            ),
            # NO eos message.
        ])

        await processor.start()
        try:
            await manager.handle_connection(ws)
            await drain_processor(processor)
        finally:
            await processor.stop()

        phrase1 = ["this", "should", "not", "be", "P1"]
        phrase2 = ["and", "it", "should", "be", "one", "of", "the", "children", "of"]

        def insert(word: str) -> Tuple[str, str, Dict[str, Any]]:
            return ("request", "intelligent_insert_text", {"insertion_string": word})

        def lifecycle(action: str) -> Tuple[str, str, Dict[str, Any]]:
            return ("command", action, {"utterance_id": utt})

        expected = (
            [lifecycle("start_utterance")]
            + [insert(w) for w in phrase1]
            + [lifecycle("end_utterance"), lifecycle("start_utterance")]
            + [insert(w) for w in phrase2]
            + [lifecycle("end_utterance")]
            # Disconnect cleanup: remove_client queues one more end marker
            # because UTT-67 is still the current utterance when the fake
            # websocket stream ends.
            + [lifecycle("end_utterance")]
        )
        assert app.calls == expected

        # Redundant with the exact-match above, but states the wh-58vf.5
        # hazard directly: the reset pair lands strictly after phrase 1's
        # last insert and strictly before phrase 2's first insert.
        reset_end = app.calls.index(lifecycle("end_utterance"))
        second_start = app.calls.index(lifecycle("start_utterance"), reset_end)
        last_p1_insert = app.calls.index(insert("P1"))
        first_p2_insert = app.calls.index(insert("and"))
        assert last_p1_insert < reset_end < second_start < first_p2_insert

        # No retraction IPC anywhere.
        assert not any(action == "retract" for _, action, _ in app.calls)
