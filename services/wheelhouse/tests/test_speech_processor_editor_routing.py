"""Integration test for the persistent-editor cut-over (wh-g2-refactor.18 slice 18.32.1).

Locks in the post-slice-18.32.1 contract: a DICTATION word arriving
while the focus-redirect policy reports terminal-at-prompt must route
to ``LogicController.insert_editor_word`` and MUST NOT also fire the
legacy ``intelligent_insert_text`` IPC. The companion expectation is
the inverse: a non-terminal focus must fall through to
``intelligent_insert_text`` and MUST NOT call ``insert_editor_word``.

Without these tests the slice 18.32.1 cut-over could regress silently
back to the slice-pre-18.32.1 state (the IPC plumbing landed but no
production call site) -- which is exactly what deepseek caught as
wh-g2-refactor.32.1.
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
) -> tuple[SpeechProcessor, _MockApp, MagicMock]:
    """Construct a SpeechProcessor wired to a deterministic policy.

    The mirror is a fresh stub (always CLOSED, matching production).
    The HWND resolution and the prompt detector are stubbed so the
    test does not depend on Win32 state. ``process_name`` controls
    whether the policy classifies the focus as a terminal.
    ``detector_return`` is the value the stub prompt-detector returns
    when called.
    """
    mirror = LogicMirror()

    # Stub the HWND-to-process resolution boundary inside the policy
    # module so the policy never touches real Win32.
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
    text_parser = TextParser(MagicMock(app=app), catalog)
    logic_controller = MagicMock()
    # wh-editor-retract-dup: insert_editor_word now returns the char count
    # the editor inserted; the speech processor accumulates that into
    # _editor_chars_this_utterance. The 1:1 stub mirrors a BMP word with no
    # leading space (the first word of an utterance).
    logic_controller.insert_editor_word = AsyncMock(
        side_effect=lambda text, utterance_id: len(text)
    )
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
    return proc, app, logic_controller


@pytest.mark.asyncio
async def test_dictation_routes_to_editor_when_terminal_at_prompt(monkeypatch):
    """Terminal focus + prompt-detector True -> insert_editor_word IPC fires.

    The legacy intelligent_insert_text IPC MUST NOT also fire. This is
    the load-bearing test that would have caught deepseek's
    wh-g2-refactor.32.1 finding.
    """
    proc, app, logic_controller = _build_processor(
        detector_return=True,
        process_name="WindowsTerminal.exe",
        monkeypatch=monkeypatch,
    )

    word = WordEvent(
        word="hello",
        start_of_utterance=True,
        end_of_utterance=False,
        utterance_id=42,
    )
    await proc.process_word_event(word)

    # insert_editor_word fired exactly once with the word text and
    # the stringified utterance id.
    assert logic_controller.insert_editor_word.await_count == 1
    args, _ = logic_controller.insert_editor_word.await_args
    assert args == ("hello", "42")

    # No legacy intelligent_insert_text IPC was emitted.
    legacy_calls = [
        a for a in app.actions
        if a.get("action") == "intelligent_insert_text"
    ]
    assert legacy_calls == []

    # Per-utterance editor accounting was updated.
    assert proc._used_editor_this_utterance is True
    assert proc._editor_chars_this_utterance == len("hello")


@pytest.mark.asyncio
async def test_dictation_falls_through_when_focus_is_not_terminal(monkeypatch):
    """Non-terminal focus -> intelligent_insert_text fires; editor IPC does not.

    Mirror-image of the routing test above. Without this guard the
    policy's accept tier could over-fire and silently send notepad
    dictation into the persistent editor.
    """
    proc, app, logic_controller = _build_processor(
        detector_return=True,  # Detector says "at prompt" but...
        process_name="notepad.exe",  # ...not a terminal, so policy declines.
        monkeypatch=monkeypatch,
    )

    word = WordEvent(
        word="hello",
        start_of_utterance=True,
        end_of_utterance=False,
        utterance_id=43,
    )
    await proc.process_word_event(word)

    assert logic_controller.insert_editor_word.await_count == 0

    legacy_calls = [
        a for a in app.actions
        if a.get("action") == "intelligent_insert_text"
    ]
    assert len(legacy_calls) == 1
    assert legacy_calls[0]["params"]["insertion_string"] == "hello"

    assert proc._used_editor_this_utterance is False
    assert proc._editor_chars_this_utterance == 0
