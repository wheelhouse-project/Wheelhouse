"""The editor restore never asks the Input side's start count.

wh-spaced-punctuation-names-unresolved.3.1.9 (codex round 6). The guard
d1b003f6 added applies the Input process's paste-counter generation to
the EDITOR prepend, and that count has no authority over the editor's
credit ledger. The ledger's session comes from its own utterance id --
``show_editor`` seeds it (terminal_editor_window.py:411 and :413) and
``insert_word`` binds it on the implicit-start path (:611) -- while the
count is moved by ``start_utterance``, whose only Input-side effect is
``UIActionHandler.start_utterance`` resetting the clipboard paste
counter. So a later ``start_utterance`` moves the count without touching
the ledger, and the whole-utterance editor retract still peels the
earlier words' runs while the guard suppresses their replay: the words
leave the document and never come back.

These tests run the real ``SpeechProcessor``, the real ``TextParser``
and pattern catalog, and a REAL ``TerminalDictationEditorWindow`` with
its real ``QPlainTextEdit`` document and ``CreditLedger`` behind a small
controller adapter that forwards the two IPC calls to the window's own
methods. The window is never shown, so the whole measurement stays
offscreen. What is asserted is the document text the Qt deletion and
replay actually leave behind, not a call argument.

Restoring unconditionally cannot double text. The decision is made
before the retract and so cannot know the ledger's answer, but it does
not need to: on a session mismatch ``CreditLedger.retract_all_and_replay``
returns ``RetractResult(0, 0, FAILURE_SESSION_MISMATCH)``
(shared/ledger.py:445) -- it removes nothing and installs nothing, so
the replay text never reaches the document at all. The last test here
measures exactly that.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

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
from services.wheelhouse.speech.focus_redirect_policy import (  # noqa: E402
    RedirectDecision,
)


class _CountingApp:
    """The app object, counting ``start_utterance`` the way the real one does.

    ``WheelHouseApp.send_command`` bumps ``utterance_start_generation``
    immediately before the enqueue, for every producer of the command
    (app.py:1131-1133). Nothing else moves the number, and it only ever
    rises.
    """

    def __init__(self) -> None:
        self.utterance_start_generation = 0
        self.commands: List[Dict[str, Any]] = []
        self.requests: List[Dict[str, Any]] = []

    async def send_command(self, payload: dict) -> bool:
        if payload.get("action") == "start_utterance":
            self.utterance_start_generation += 1
        self.commands.append(payload)
        return True

    async def send_request(
        self, action: str, params: Optional[dict] = None,
    ) -> dict:
        self.requests.append({"action": action, "params": params})
        return {"status": "success"}


class _SpeechHandler:
    """The object TextParser reaches the app and the processor through.

    ``command_engine._execute_rule`` reads ``speech_processor`` off it
    to route a replacement's text-insertion step into the editor
    (speech/command_engine.py:398-419); a handler without that
    attribute sends ``intelligent_insert_text`` instead, which is the
    legacy surface and not what these tests measure.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.speech_processor = None


class _AlwaysRedirectPolicy:
    """The terminal-prompt decision, fixed to its normal redirect answer."""

    def __init__(self) -> None:
        self.calls = 0

    async def should_redirect(self, focused_hwnd: int) -> RedirectDecision:
        self.calls += 1
        return RedirectDecision(
            open_editor=True,
            target_terminal_hwnd=focused_hwnd,
            reason="terminal_at_prompt",
        )

    def on_utterance_end(self) -> None:
        return None


class _RealEditorController:
    """Forwards the two editor IPCs straight to the real window.

    ``LogicController.insert_editor_word`` returns the GUI's
    ``clusters_inserted`` (main.py:10385-10398) and
    ``LogicController.retract_editor_text`` returns True only when the
    GUI reported an empty ``failure_reason`` (main.py:10442-10459).
    ``EditorIpcResponder`` reads both off the window's own return value
    (shared/editor_ipc_responder.py:188 and :298), so forwarding to the
    window and reading the same two fields is the whole transport this
    adapter stands in for. Focus acquisition and the queue hop are what
    it leaves out; neither can change what the ledger peels.
    """

    def __init__(self, window) -> None:
        self.window = window
        self.shows: List[int] = []
        self.retracts: List[Dict[str, Any]] = []

    def show_editor_persistent(self, terminal_hwnd: int) -> None:
        # Deliberately does NOT call show_editor: the window stays
        # hidden so the test never puts a dialog on the user's screen,
        # and insert_word's implicit-start path (:611) binds the ledger
        # session exactly as it does when show_editor seeded an empty
        # id.
        self.shows.append(terminal_hwnd)

    async def insert_editor_word(self, text: str, utterance_id: str) -> int:
        result = self.window.insert_word(text, utterance_id)
        return int(getattr(result, "clusters_inserted", 0) or 0)

    async def retract_editor_text(
        self,
        chars_requested: int,
        utterance_id: str,
        replay_text: str = "",
        whole_utterance: bool = False,
    ) -> bool:
        result = self.window.retract_and_replay(
            int(chars_requested),
            utterance_id=utterance_id,
            replay_text=replay_text,
            whole_utterance=whole_utterance,
        )
        self.retracts.append({
            "chars_requested": chars_requested,
            "utterance_id": utterance_id,
            "replay_text": replay_text,
            "chars_removed": result.chars_removed,
            "replay_chars": result.replay_chars,
            "failure_reason": result.failure_reason,
        })
        return not result.failure_reason


@pytest.fixture
def editor_window(qtbot):
    from terminal_editor_window import TerminalDictationEditorWindow
    window = TerminalDictationEditorWindow()
    qtbot.addWidget(window)
    yield window
    window.close()


@pytest.fixture
def stand(editor_window):
    """The processor, its app, and the controller wired to the window."""
    catalog = PatternCatalog(str(patterns_path))
    app = _CountingApp()
    handler = _SpeechHandler(app)
    text_parser = TextParser(handler, catalog)
    controller = _RealEditorController(editor_window)
    proc = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=text_parser,
        app=app,
        replacement_timeout_ms=400,
        command_timeout_ms=1000,
        logic_controller=controller,
        focus_redirect_policy=_AlwaysRedirectPolicy(),
        focused_hwnd_provider=lambda: 0x1234,
    )
    handler.speech_processor = proc
    return proc, app, controller, editor_window


async def _start_utterance(app, utterance_id: int) -> None:
    """Send the command production sends before an utterance's first word.

    ``WebSocketManager`` sends it before the first word of every new
    utterance (integrations/websocket_manager.py:1292 and :1527), and
    ``_CountingApp.send_command`` bumps the count for it exactly as
    ``WheelHouseApp.send_command`` does.
    """
    await app.send_command({
        "action": "start_utterance",
        "params": {"utterance_id": utterance_id},
    })


async def _send(proc, event) -> None:
    """Queue one event and let the processing loop take it."""
    await proc.word_queue.put(event)
    await asyncio.sleep(0.01)


async def _word(
    proc, word: str, utterance_id: int, *, start: bool = False,
    generation: Optional[int] = None,
) -> None:
    await _send(proc, WordEvent(
        word=word,
        start_of_utterance=start,
        end_of_utterance=False,
        utterance_id=utterance_id,
        utterance_start_generation=generation if start else None,
    ))


async def _utterance_end(proc, utterance_id: int) -> None:
    await _send(proc, WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=True,
        utterance_id=utterance_id,
        is_utterance_end_marker=True,
    ))


async def _retraction(proc, utterance_id: int, full_text: str) -> None:
    """The Mode-3 correction marker, in the shape production sends.

    integrations/websocket_manager.py ``_handle_mode3_retract``: an
    empty word event carrying ``is_retraction_marker`` and the
    corrected final.
    """
    await _send(proc, WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=False,
        utterance_id=utterance_id,
        is_retraction_marker=True,
        retraction_full_text=full_text,
    ))


async def _hold_a_name_into_the_editor(
    proc, app, window, first_words: list[str], completing_word: str,
) -> None:
    """Run the two utterances that leave a fired mark in the editor.

    Utterance 1 says the opening words of a punctuation name and ends,
    so they are held across the boundary. Utterance 2's word completes
    the name, the mark fires, and the replacement's text-insertion step
    routes it into the editor (speech/command_engine.py:419). The held
    words are then a confirmed record of text inside the span the next
    correction will retract.
    """
    await _start_utterance(app, 1)
    for i, word in enumerate(first_words):
        await _word(proc, word, 1, start=(i == 0), generation=1)
    await _utterance_end(proc, 1)

    await _start_utterance(app, 2)
    await _word(proc, completing_word, 2, start=True, generation=2)
    # The hold's release deadline is 400 ms; the fire happens on the
    # completing word, and this waits past the deadline so a test that
    # measured the timeout path instead would still be measured here.
    await asyncio.sleep(0.6)


class TestTheEditorRestoreDoesNotAskTheInputCount:
    """The Input paste count has no authority over the editor ledger.

    Every test drives the real processor into the editor arm of
    ``_handle_retraction`` and then reads the real document.
    """

    @pytest.mark.asyncio
    async def test_a_later_start_leaves_the_earlier_word_in_the_replay(
        self, stand,
    ):
        """Codex round 6's own sequence, one earlier word.

        Utterance 1 says "question" and ends. Utterance 2 says "mark",
        the held name fires, and "?" goes into the editor. The provider
        then corrects "mark" to "Marcus", and the next stable message's
        ``start_utterance`` goes out before the processor consumes that
        correction. The editor still peels its "?" run, so "question"
        leaves the document -- and only the replay can put it back.
        """
        proc, app, controller, window = stand
        await proc.start()
        try:
            await _hold_a_name_into_the_editor(
                proc, app, window, ["question"], "mark",
            )
            assert window._text_edit.toPlainText() == "?", (
                "The held name did not fire into the editor, so there is "
                "no earlier-word contribution for a retract to remove. "
                f"Document: {window._text_edit.toPlainText()!r}"
            )
            assert proc._earlier_utterance_words_in_retract_span == [
                "question"
            ], (
                "The delivery did not confirm the earlier word, so the "
                "restore has nothing to decide about and this test "
                "measures nothing."
            )

            # The later start, sent before the queued correction is
            # consumed. It moves the Input count and touches no ledger.
            await _start_utterance(app, 3)
            assert (
                proc._earlier_utterance_words_generation
                != app.utterance_start_generation
            ), (
                "The count did not move past the record's generation, so "
                "the guard under test would keep the record for a reason "
                "that has nothing to do with the fix."
            )

            await _retraction(proc, 2, "Marcus")
            await asyncio.sleep(0.2)

            assert controller.retracts, "No editor retract was sent."
            outcome = controller.retracts[0]
            assert outcome["failure_reason"] == "", (
                "The ledger refused this retract, so it removed nothing "
                "and the document proves nothing about the restore. "
                f"Outcome: {outcome}"
            )
            assert outcome["chars_removed"] == 1, (
                "The retract did not peel the mark's run, so the earlier "
                f"word was never at risk. Outcome: {outcome}"
            )
            assert window._text_edit.toPlainText() == "question Marcus", (
                "The editor removed the earlier word's contribution and "
                "the replay left it out, so it is gone from the document "
                "with no copy anywhere on screen. The Input start count "
                "moved, but it governs the Input paste counter, not this "
                f"ledger. Document: {window._text_edit.toPlainText()!r}"
            )
        finally:
            await proc.stop()

    @pytest.mark.asyncio
    async def test_a_later_start_leaves_two_earlier_words_in_the_replay(
        self, stand,
    ):
        """The same sequence with a multiword earlier candidate.

        Utterance 1 says "open single" and ends; utterance 2's "quote"
        completes the name and fires one apostrophe into the editor.
        Both held words are in the record, and both must survive the
        correction.
        """
        proc, app, controller, window = stand
        await proc.start()
        try:
            await _hold_a_name_into_the_editor(
                proc, app, window, ["open", "single"], "quote",
            )
            fired = window._text_edit.toPlainText()
            assert len(fired) == 1, (
                "The held name did not fire a single mark into the "
                f"editor. Document: {fired!r}"
            )
            assert proc._earlier_utterance_words_in_retract_span == [
                "open", "single",
            ], (
                "Both held words must be in the record; the restore's "
                "scope is what this test measures."
            )

            await _start_utterance(app, 3)
            await _retraction(proc, 2, "coat")
            await asyncio.sleep(0.2)

            assert controller.retracts, "No editor retract was sent."
            outcome = controller.retracts[0]
            assert outcome["failure_reason"] == "", (
                f"The ledger refused this retract. Outcome: {outcome}"
            )
            assert window._text_edit.toPlainText() == "open single coat", (
                "Both earlier words left the document with the peeled "
                "run and the replay did not bring them back. "
                f"Document: {window._text_edit.toPlainText()!r}"
            )
        finally:
            await proc.stop()

    @pytest.mark.asyncio
    async def test_a_refused_retract_installs_nothing_the_restore_added(
        self, stand,
    ):
        """The unconditional restore cannot double text.

        This is the reason the editor arm needs no count guard. The
        restore decides before the retract and cannot know the ledger's
        answer, but on a session mismatch
        ``CreditLedger.retract_all_and_replay`` returns
        ``RetractResult(0, 0, FAILURE_SESSION_MISMATCH)``
        (shared/ledger.py:445): it peels nothing and it installs
        nothing, so the restored words never reach the document.

        The mismatch here is the one ``show_editor`` produces -- it
        seeds the ledger with the new session's id
        (terminal_editor_window.py:411 and :413) -- arriving while the
        processor still holds the previous utterance's id.
        """
        from services.wheelhouse.shared.ledger import (
            FAILURE_SESSION_MISMATCH,
        )

        proc, app, controller, window = stand
        await proc.start()
        try:
            await _hold_a_name_into_the_editor(
                proc, app, window, ["question"], "mark",
            )
            assert window._text_edit.toPlainText() == "?"

            # A fresh editor session takes the ledger, exactly as a new
            # show_editor does. The processor's retract still carries
            # the old utterance id.
            window._ledger.start_utterance("99")

            await _start_utterance(app, 3)
            await _retraction(proc, 2, "Marcus")
            await asyncio.sleep(0.2)

            assert controller.retracts, "No editor retract was sent."
            outcome = controller.retracts[0]
            assert "question" in outcome["replay_text"], (
                "The restore did not put the earlier word into the "
                "replay, so this test is not measuring what an "
                "unconditional restore hands a refusing ledger. "
                f"Outcome: {outcome}"
            )
            assert outcome["failure_reason"] == FAILURE_SESSION_MISMATCH, (
                f"Expected a session mismatch. Outcome: {outcome}"
            )
            assert outcome["chars_removed"] == 0, (
                f"A refusing ledger must peel nothing. Outcome: {outcome}"
            )
            assert outcome["replay_chars"] == 0, (
                f"A refusing ledger must install nothing. Outcome: {outcome}"
            )
            assert window._text_edit.toPlainText() == "?", (
                "The refused retract changed the document, so the "
                "restored words reached the screen beside the copy the "
                "user can already see. "
                f"Document: {window._text_edit.toPlainText()!r}"
            )
        finally:
            await proc.stop()
