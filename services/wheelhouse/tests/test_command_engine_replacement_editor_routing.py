"""Replacement text routes through the editor at a terminal prompt.

Without this routing, a first-word replacement like "period" produces
"." via ``intelligent_insert_text`` and the "." lands in the shell
because the speech processor's DICTATE path never runs for a matched
replacement. The fix in ``command_engine._execute_rule`` consults the
focus-redirect policy and, when at a terminal prompt, sends the text to
the persistent editor via ``insert_editor_word`` instead.

These tests lock in the new contract:

* Terminal focus + prompt-detector True + replacement pattern match
  -> ``insert_editor_word`` fires with the replacement's text, and
  the legacy ``intelligent_insert_text`` IPC MUST NOT also fire.

* Non-terminal focus + replacement pattern match
  -> ``intelligent_insert_text`` fires with the replacement's text,
  and ``insert_editor_word`` MUST NOT fire.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

# Path setup matching the other speech_processor test modules.
test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
patterns_path = wheelhouse_dir / "speech" / "config" / "patterns.toml"
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

from speech.command_engine import TextParser  # noqa: E402
from speech.pattern_catalog import PatternCatalog  # noqa: E402
from speech.speech_processor import SpeechProcessor  # noqa: E402
from speech.word_event import WordEvent  # noqa: E402
from services.wheelhouse.shared.editor_lifecycle import (  # noqa: E402
    LogicMirror,
)
from services.wheelhouse.speech.focus_redirect_policy import (  # noqa: E402
    FocusRedirectPolicy,
)


class _MockApp:
    """Records send_command and send_request payloads."""

    def __init__(self) -> None:
        self.actions: List[Dict[str, Any]] = []

    async def send_command(self, payload: dict) -> None:
        self.actions.append({"kind": "command", **payload})

    async def send_request(self, action: str, params: dict) -> dict:
        self.actions.append({"kind": "request", "action": action, "params": params})
        return {"status": "success"}


class _MockContextMirror:
    def init_reader(self) -> None:
        return None

    def read_context(self) -> dict:
        return {"app_name": "TestApp", "window_title": "Test", "timestamp": 0.0}


def _build_processor(
    *,
    detector_return: bool,
    process_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SpeechProcessor, _MockApp, MagicMock, TextParser]:
    """Construct a SpeechProcessor wired to a deterministic policy.

    Mirrors ``test_speech_processor_editor_routing._build_processor`` but
    also returns the TextParser and links it to the SpeechProcessor via
    a stand-in speech_handler so the replacement path can reach
    ``speech_processor.maybe_route_to_editor`` from
    ``command_engine._execute_rule``.
    """
    mirror = LogicMirror()

    import services.wheelhouse.speech.focus_redirect_policy as policy_mod

    monkeypatch.setattr(
        policy_mod,
        "process_name_for_hwnd",
        lambda hwnd: process_name,
    )

    class _StubWin32Process:
        @staticmethod
        def GetWindowThreadProcessId(hwnd):  # noqa: N802 - mimic win32 api
            return (1234, 4242)

    monkeypatch.setattr(policy_mod, "win32process", _StubWin32Process)

    detector_call = MagicMock(return_value=detector_return)
    policy = FocusRedirectPolicy(
        mirror=mirror,
        prompt_detector_call=detector_call,
        detector_timeout_s=1.0,
    )

    catalog = PatternCatalog(str(patterns_path))
    app = _MockApp()

    # The speech_handler stub exposes ``app`` and a placeholder
    # ``speech_processor`` so TextParser._execute_rule can reach the
    # processor's ``maybe_route_to_editor`` after it is wired below.
    speech_handler_stub = MagicMock(app=app)
    text_parser = TextParser(speech_handler_stub, catalog)

    logic_controller = MagicMock()
    logic_controller.insert_editor_word = AsyncMock()
    logic_controller.show_editor_persistent = MagicMock()
    logic_controller.retract_editor_text = AsyncMock()

    proc = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=text_parser,
        app=app,
        replacement_timeout_ms=400,
        command_timeout_ms=1000,
        logic_controller=logic_controller,
        focus_redirect_policy=policy,
        focused_hwnd_provider=lambda: 0x1234,
    )
    proc.context_mirror = _MockContextMirror()

    # Link the stub speech_handler back to the real processor so the
    # command_engine intercept can call processor.maybe_route_to_editor.
    speech_handler_stub.speech_processor = proc

    return proc, app, logic_controller, text_parser


@pytest.mark.asyncio
async def test_replacement_routes_to_editor_when_terminal_at_prompt(monkeypatch):
    """First-word replacement at terminal prompt routes "." into the editor.

    The "period" replacement pattern fires the ``text`` action with
    "." as the template. Without the command_engine intercept the
    pattern would dispatch ``intelligent_insert_text`` and the "."
    would land in the terminal. With the intercept the editor opens
    and the "." is sent via ``insert_editor_word``.
    """
    proc, app, logic_controller, _text_parser = _build_processor(
        detector_return=True,
        process_name="WindowsTerminal.exe",
        monkeypatch=monkeypatch,
    )

    word = WordEvent(
        word="period",
        start_of_utterance=True,
        end_of_utterance=False,
        utterance_id=99,
    )
    await proc.process_word_event(word)

    # The router will buffer "period" first. Drive the timeout-finalize
    # path so the replacement actually executes.
    end_marker = WordEvent.timeout_finalize(token=proc.timeout_token)
    await proc.process_word_event(end_marker)

    # The replacement should have routed through the editor.
    assert logic_controller.insert_editor_word.await_count == 1
    args, _ = logic_controller.insert_editor_word.await_args
    assert args == (".", "99")

    # show_editor_persistent fired once to reveal the editor.
    assert logic_controller.show_editor_persistent.call_count == 1

    # No legacy intelligent_insert_text IPC was emitted by the
    # replacement's text-insert step.
    legacy_calls = [
        a for a in app.actions
        if a.get("action") == "intelligent_insert_text"
    ]
    assert legacy_calls == []

    # Per-utterance editor accounting reflects the routed insert.
    assert proc._used_editor_this_utterance is True
    assert proc._editor_chars_this_utterance == 1


@pytest.mark.asyncio
async def test_replacement_falls_through_when_focus_is_not_terminal(monkeypatch):
    """Replacement output uses intelligent_insert_text when focus is not a terminal.

    Mirror-image of the test above. Without this guard the new
    command_engine intercept could over-fire and silently send
    notepad replacements into the persistent editor.
    """
    proc, app, logic_controller, _text_parser = _build_processor(
        detector_return=True,  # Detector says "at prompt" but...
        process_name="notepad.exe",  # ...not a terminal, so policy declines.
        monkeypatch=monkeypatch,
    )

    word = WordEvent(
        word="period",
        start_of_utterance=True,
        end_of_utterance=False,
        utterance_id=100,
    )
    await proc.process_word_event(word)

    end_marker = WordEvent.timeout_finalize(token=proc.timeout_token)
    await proc.process_word_event(end_marker)

    # No editor IPC.
    assert logic_controller.insert_editor_word.await_count == 0
    assert logic_controller.show_editor_persistent.call_count == 0

    # The replacement's text-insert step still ran via the normal IPC.
    legacy_calls = [
        a for a in app.actions
        if a.get("action") == "intelligent_insert_text"
    ]
    assert len(legacy_calls) == 1
    assert legacy_calls[0]["params"]["insertion_string"] == "."

    assert proc._used_editor_this_utterance is False
    assert proc._editor_chars_this_utterance == 0
