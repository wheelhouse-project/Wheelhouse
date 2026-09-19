"""Closed-grid dictation fallbacks stay retractable (wh-mouse-grid.1.27).

With the grid closed, the bare grid words (mark / drag / move here, the
click variants, and the 1-9 numbers) match their whole-utterance command
patterns, and the actions layer falls back to plain dictation. That
fallback must ride ``SpeechProcessor._send_to_dictation`` -- the path
that applies the terminal focus-redirect (``maybe_route_to_editor``) and
the per-utterance editor bookkeeping the retraction path reads -- and
the match must NOT be recorded as an executed command, or a later STT
revision of the utterance can never retract the typed words.

These tests drive the REAL TextParser over the REAL pattern catalog
through ``SpeechProcessor._execute_command``, with a fake
LogicController whose ``handle_grid_command`` reports the grid closed
(consumed=False). They are the integration coverage the direct
ActionFunctions tests in test_grid_speech_routing.py section 6 cannot
give: those never exercise TextParser or SpeechProcessor, so they stay
green even when the match is wrongly marked a command.
"""
import sys
from pathlib import Path

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from speech.command_engine import TextParser
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from speech.word_event import WordEvent


class _RecordingApp:
    """Records send_command payloads and send_request calls."""

    def __init__(self):
        self.commands: List[dict] = []
        self.requests: List[dict] = []
        self.retract_response: Dict[str, Any] = {
            "status": "retracted", "chars": 4,
        }

    async def send_command(self, payload: dict):
        self.commands.append(payload)

    async def send_request(
        self, action: str, params: Optional[dict] = None,
        timeout_s: Optional[float] = None,
    ):
        payload = {"action": action, "params": params or {}}
        self.requests.append(payload)
        if action == "retract":
            return self.retract_response
        return {"status": "ok"}

    def inserts(self) -> List[str]:
        return [
            r["params"]["insertion_string"] for r in self.requests
            if r["action"] == "intelligent_insert_text"
        ]


class _ClosedGridLc:
    """handle_grid_command reporting the grid CLOSED (never consumes)."""

    def __init__(self):
        self.calls = []

    async def handle_grid_command(self, command, trace_id, **kwargs):
        self.calls.append((command, kwargs))
        return False


def _make_processor():
    app = _RecordingApp()
    catalog = PatternCatalog("speech/config/patterns.toml")
    speech_handler = MagicMock()
    speech_handler.app = app
    speech_handler.logic_controller = _ClosedGridLc()
    parser = TextParser(speech_handler, catalog)
    proc = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=parser,
        app=app,
        replacement_timeout_ms=700,
        command_timeout_ms=1000,
        hotword=catalog.command_hotword,
    )
    speech_handler.speech_processor = proc
    return proc, app


FALLBACK_UTTERANCES = [
    "mark",
    "drag",
    "move here",
    "click",
    "right click",
    "double click",
    "five",
    "3",
    "number five",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", FALLBACK_UTTERANCES)
async def test_closed_grid_fallback_dictates_and_is_not_a_command(utterance):
    proc, app = _make_processor()

    await proc._execute_command(utterance)

    # The words typed through the normal dictation path (the awaited
    # intelligent_insert_text request _send_to_dictation makes), not a
    # raw fire-and-forget payload.
    assert app.inserts() == [utterance]
    assert app.commands == []
    # The match is recorded as a dictation fallback, not a command, so
    # retraction stays possible.
    assert proc.text_parser.last_executed_pattern_type == "dictation_fallback"
    assert proc._command_executed_in_utterance is False


@pytest.mark.asyncio
async def test_closed_grid_fallback_then_retraction_replays_the_final():
    # Real sequence from the finding: grid closed; STT finalizes "mark"
    # and it is typed; a later retraction revises it to "march". The
    # retraction must run (retract IPC) and the corrected word must
    # replay -- before the fix the fallback was marked a command and
    # _handle_retraction returned early, leaving "mark" on screen.
    proc, app = _make_processor()

    await proc._execute_command("mark")
    assert app.inserts() == ["mark"]

    marker = WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=False,
        utterance_id=1,
        is_retraction_marker=True,
        retraction_full_text="march",
    )
    await proc.process_word_event(marker)

    retracts = [r for r in app.requests if r["action"] == "retract"]
    assert len(retracts) == 1
    assert app.inserts() == ["mark", "march"]


@pytest.mark.asyncio
async def test_closed_grid_fallback_routes_through_the_editor_redirect():
    # At a terminal prompt the focus-redirect policy opens the hidden
    # editor; the fallback word must land there (insert_editor_word),
    # not type into the terminal. Before the fix the fallback bypassed
    # _send_to_dictation entirely, so the redirect never ran.
    proc, app = _make_processor()
    proc._current_utterance_id = 7

    inserted: List[str] = []

    class _EditorLc:
        async def insert_editor_word(self, text, utterance_str):
            inserted.append(text)
            return len(text)

        def show_editor_persistent(self, hwnd):
            pass

    proc.logic_controller = _EditorLc()

    class _Policy:
        async def should_redirect(self, hwnd):
            return SimpleNamespace(
                open_editor=True, target_terminal_hwnd=42,
            )

    proc.focus_redirect_policy = _Policy()
    proc._focused_hwnd_provider = lambda: 99

    await proc._execute_command("mark")

    assert inserted == ["mark"]
    assert app.inserts() == []
    assert proc._command_executed_in_utterance is False


@pytest.mark.asyncio
async def test_a_timed_out_fallback_insert_is_not_retried():
    # wh-mouse-grid.1.28: when the insert request reaches Input but its
    # acknowledgement arrives after the Logic timeout, send_request
    # raises TimeoutError AFTER the insert was submitted. If that error
    # escapes the fallback, _execute_rule reports the rule as failed,
    # _execute_command reads that as "no pattern matched", and it
    # dictates the same words a second time -- both insertions can run.
    # The fallback must absorb the error: the words were submitted once.
    proc, app = _make_processor()

    first_insert = {"pending": True}
    orig_send_request = app.send_request

    async def late_ack(action, params=None, timeout_s=None):
        result = await orig_send_request(action, params, timeout_s)
        if action == "intelligent_insert_text" and first_insert["pending"]:
            first_insert["pending"] = False
            raise asyncio.TimeoutError()
        return result

    app.send_request = late_ack

    await proc._execute_command("mark")

    assert app.inserts() == ["mark"]
    assert app.commands == []
    assert proc.text_parser.last_executed_pattern_type == "dictation_fallback"
    assert proc._command_executed_in_utterance is False


@pytest.mark.asyncio
async def test_a_real_command_after_a_fallback_still_blocks_retraction():
    # The fallback flag is per-parse state: it must reset at the top of
    # every parse_and_execute call. A stale True would mark the NEXT
    # real command as a dictation fallback, letting a retraction undo
    # past an irreversible side effect.
    proc, app = _make_processor()

    await proc._execute_command("mark")
    assert proc._command_executed_in_utterance is False

    await proc._execute_command("press enter")

    assert proc.text_parser.last_executed_pattern_type == "command"
    assert proc._command_executed_in_utterance is True
