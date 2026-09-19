"""A restore puts words back only on the surface that delivered them.

wh-spaced-punctuation-names-unresolved.3.1.10 (codex round 7). The
confirmed earlier-word record stores the words and the delivery's
start_utterance count, and nothing else:
``_record_delivered_earlier_utterance_words`` takes ``surface`` as its
first argument and never keeps it (speech_processor.py:3682-3719). Both
restoration decisions then answer a question that is not the one they
need to ask.

  * The EDITOR arm restores unconditionally
    (``_restore_earlier_utterance_words``, :3392-3434). Its written
    defence is that a wrong replay is refused by
    ``CreditLedger.retract_all_and_replay`` returning
    ``RetractResult(0, 0, FAILURE_SESSION_MISMATCH)``
    (shared/ledger.py:445). That covers a mismatched session and
    nothing else: when the earlier words went out on the LEGACY path
    in the same generation, the sessions MATCH, the ledger peels only
    the correction's own characters, and the earlier words are typed a
    second time beside the copy the user can still see.
  * The LEGACY arm restores while ``recorded == current``
    (``_earlier_words_still_in_live_span``, :3316-3390). That proves
    the Input paste span was never reset. It does not prove the span
    CONTAINS those words: when they went to the editor, the legacy
    retract removes nothing of theirs and the restore adds a second
    copy.

A session id and a paste count are two separate accounting systems.
Neither says which surface a word landed on, so neither can decide
whether a retract owns it.

The surface can change between the delivery and the correction without
any contrived input. Editor routing is sticky for one utterance only --
``_used_editor_this_utterance`` is reset at :1566 on every
start-of-utterance word, and ``maybe_route_to_editor`` (:4377-4465)
re-asks the focus policy for every word while that flag is False. So a
user who clicks a terminal between two utterances is enough, and so is
the app's own foreground change when ``show_editor`` calls
``_steal_foreground`` (terminal_editor_window.py:443).

These tests run the real ``SpeechProcessor``, the real ``TextParser``
and pattern catalog, and a REAL ``TerminalDictationEditorWindow`` with
its own ``QPlainTextEdit`` document and ``CreditLedger`` behind a small
controller adapter, exactly as
``test_speech_processor_editor_restore_unconditional.py`` does. The
window is never shown, so the measurement stays offscreen. What is
asserted is the text the real Qt deletion and replay leave behind, and
the text the legacy path really sent.
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


class _RecordingApp:
    """The app object, counting starts and recording every legacy send.

    ``WheelHouseApp.send_command`` bumps ``utterance_start_generation``
    immediately before the enqueue, for every producer of the command
    (app.py:1131-1133), and nothing else moves the number.

    Legacy dictation reaches the Input process as
    ``intelligent_insert_text`` down TWO routes, and both must be
    recorded or a measurement misses half the traffic. A dictated word
    is awaited (``send_request``, speech_processor.py:4332-4335); a
    replacement step's own text is fire-and-forget (``send_command``,
    from the step executor in command_engine.py). Both are the same
    surface, so ``sends`` keeps them in one ordered list. Measured
    2026-09-06: a fired mark arrives only through ``send_command``.

    The legacy correction is ``send_request(action='retract',
    params={})`` (:4138). The retract answers ``retracted`` here
    because every other status returns before the restoration this
    file is about.
    """

    def __init__(self) -> None:
        self.utterance_start_generation = 0
        self.commands: List[Dict[str, Any]] = []
        self.requests: List[Dict[str, Any]] = []
        self.sends: List[Dict[str, Any]] = []

    def _note(self, action: str, params: Optional[dict]) -> None:
        self.sends.append({"action": action, "params": params})

    async def send_command(self, payload: dict) -> bool:
        if payload.get("action") == "start_utterance":
            self.utterance_start_generation += 1
        self.commands.append(payload)
        self._note(payload.get("action", ""), payload.get("params"))
        return True

    async def send_request(
        self, action: str, params: Optional[dict] = None,
    ) -> dict:
        self.requests.append({"action": action, "params": params})
        self._note(action, params)
        if action == "retract":
            return {"status": "retracted", "text": ""}
        return {"status": "success"}

    def legacy_texts(self) -> List[str]:
        """Every string the legacy dictation path really sent, in order."""
        return [
            (s["params"] or {}).get("insertion_string", "")
            for s in self.sends
            if s["action"] == "intelligent_insert_text"
        ]


class _SpeechHandler:
    """The object TextParser reaches the app and the processor through."""

    def __init__(self, app) -> None:
        self.app = app
        self.speech_processor = None


class _SwitchingPolicy:
    """The focus decision, answerable differently at different moments.

    The real ``FocusRedirectPolicy`` answers from the focused window,
    so its answer changes when the foreground window changes. Setting
    ``redirect`` is how these tests move focus: nothing else about the
    policy differs from ``_AlwaysRedirectPolicy`` in the .3.1.9 file.
    """

    def __init__(self, redirect: bool = True) -> None:
        self.redirect = redirect
        self.calls = 0

    async def should_redirect(self, focused_hwnd: int) -> RedirectDecision:
        self.calls += 1
        if not self.redirect:
            return RedirectDecision(
                open_editor=False,
                target_terminal_hwnd=0,
                reason="not_a_terminal",
            )
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
    GUI reported an empty ``failure_reason`` (main.py:10442-10459), so
    forwarding to the window and reading those two fields is the whole
    transport this adapter stands in for.
    """

    def __init__(self, window) -> None:
        self.window = window
        self.shows: List[int] = []
        self.retracts: List[Dict[str, Any]] = []

    def show_editor_persistent(self, terminal_hwnd: int) -> None:
        # Deliberately does NOT call show_editor: the window stays
        # hidden, and insert_word's implicit-start path
        # (terminal_editor_window.py:611) binds the ledger session
        # exactly as it does when show_editor seeded an empty id.
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
    """The processor, its app, the controller, the window and the policy."""
    catalog = PatternCatalog(str(patterns_path))
    app = _RecordingApp()
    handler = _SpeechHandler(app)
    text_parser = TextParser(handler, catalog)
    controller = _RealEditorController(editor_window)
    policy = _SwitchingPolicy(redirect=True)
    proc = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=text_parser,
        app=app,
        replacement_timeout_ms=400,
        command_timeout_ms=1000,
        logic_controller=controller,
        focus_redirect_policy=policy,
        focused_hwnd_provider=lambda: 0x1234,
    )
    handler.speech_processor = proc
    return proc, app, controller, editor_window, policy


async def _start_utterance(app, utterance_id: int) -> None:
    """The command production sends before an utterance's first word.

    ``WebSocketManager`` sends it before the first word of every new
    utterance (integrations/websocket_manager.py:1292 and :1527).
    """
    await app.send_command({
        "action": "start_utterance",
        "params": {"utterance_id": utterance_id},
    })


async def _send(proc, event) -> None:
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
    """The Mode-3 correction marker, in the shape production sends."""
    await _send(proc, WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=False,
        utterance_id=utterance_id,
        is_retraction_marker=True,
        retraction_full_text=full_text,
    ))


async def _hold_across_the_boundary(
    proc, app, first_words: list[str], completing_word: str,
) -> None:
    """Say a punctuation name's opening words, end, then complete it.

    Utterance 1's words are held across the boundary. Utterance 2's
    word completes the name, so the mark fires and its insertion step
    delivers on whichever surface the policy allows at that moment.
    That delivery is what confirms the earlier words as a record.
    """
    await _start_utterance(app, 1)
    for i, word in enumerate(first_words):
        await _word(proc, word, 1, start=(i == 0), generation=1)
    await _utterance_end(proc, 1)

    await _start_utterance(app, 2)
    await _word(proc, completing_word, 2, start=True, generation=2)
    # The hold's release deadline is 400 ms. Waiting past it means a
    # run that took the timeout path instead of the fire is still
    # measured here rather than silently skipped.
    await asyncio.sleep(0.6)


class TestARestoreNeverCrossesToTheOtherSurface:
    """Earlier words come back only through the retract that owns them."""

    @pytest.mark.asyncio
    async def test_the_editor_replay_omits_words_the_legacy_path_delivered(
        self, stand,
    ):
        """Case A of .3.1.10, the fired-mark spelling, one earlier word.

        The mark fires while the focus policy declines, so "?" and the
        earlier word "question" are delivered by the LEGACY path and
        stay on the Input side. A later word in the same utterance then
        routes to the editor, which is enough to send the correction
        down the editor arm. The ledger's session matches, so it peels
        that later word's own run and installs the replay -- and the
        replay must not carry "question", because nothing removed the
        legacy copy the user can still see.
        """
        proc, app, controller, window, policy = stand
        await proc.start()
        try:
            policy.redirect = False
            await _hold_across_the_boundary(proc, app, ["question"], "mark")

            assert proc._earlier_utterance_words_in_retract_span == [
                "question"
            ], (
                "The delivery did not confirm the earlier word, so there "
                "is no record for a restore to decide about and this "
                "test measures nothing."
            )
            assert "?" in "".join(app.legacy_texts()), (
                "The mark did not go out on the legacy path, so this is "
                f"not the case under test. Legacy: {app.legacy_texts()!r}"
            )
            assert window._text_edit.toPlainText() == "", (
                "The editor already holds text, so the retract below "
                "would be peeling something this test did not put there."
            )

            # Focus moves to a terminal prompt. Editor routing is
            # re-asked for every word while _used_editor_this_utterance
            # is False, so the next word of the SAME utterance opens the
            # editor and the correction takes the editor arm.
            policy.redirect = True
            await _word(proc, "hello", 2)
            assert proc._used_editor_this_utterance, (
                "The later word did not route to the editor, so the "
                "correction would take the legacy arm and this test "
                "would measure the wrong decision."
            )

            await _retraction(proc, 2, "goodbye")
            await asyncio.sleep(0.2)

            assert controller.retracts, "No editor retract was sent."
            outcome = controller.retracts[0]
            assert outcome["failure_reason"] == "", (
                "The ledger refused this retract, so it removed nothing "
                "and the document proves nothing. "
                f"Outcome: {outcome}"
            )
            assert "question" not in outcome["replay_text"], (
                "The editor replay carries an earlier word the LEGACY "
                "path delivered. The ledger removed only this "
                "utterance's own editor run, so the legacy copy is "
                "still on screen and this replay types it a second "
                f"time. Replay: {outcome['replay_text']!r}"
            )
            assert "question" not in window._text_edit.toPlainText(), (
                "The editor document gained a word the legacy path "
                "delivered and no retract removed. "
                f"Document: {window._text_edit.toPlainText()!r}"
            )
        finally:
            await proc.stop()

    @pytest.mark.asyncio
    async def test_the_editor_replay_omits_two_words_the_legacy_path_sent(
        self, stand,
    ):
        """The multiword sibling of the case above.

        A two-word hold is the acceptance criteria's multiword case,
        and it is worth measuring separately: the record is a list, so
        a repair that keeps only its first entry would pass the
        one-word test and fail here.
        """
        proc, app, controller, window, policy = stand
        await proc.start()
        try:
            policy.redirect = False
            await _hold_across_the_boundary(
                proc, app, ["open", "single"], "quote",
            )

            earlier = proc._earlier_utterance_words_in_retract_span
            assert earlier == ["open", "single"], (
                "The delivery did not confirm both earlier words, so "
                f"the multiword case is not under test. Record: {earlier!r}"
            )
            assert window._text_edit.toPlainText() == "", (
                "The editor already holds text, so the retract below "
                "would be peeling something this test did not put there."
            )

            policy.redirect = True
            await _word(proc, "hello", 2)
            assert proc._used_editor_this_utterance, (
                "The later word did not route to the editor, so the "
                "correction would take the legacy arm."
            )

            await _retraction(proc, 2, "goodbye")
            await asyncio.sleep(0.2)

            assert controller.retracts, "No editor retract was sent."
            outcome = controller.retracts[0]
            assert outcome["failure_reason"] == "", (
                f"The ledger refused this retract. Outcome: {outcome}"
            )
            for word in ("open", "single"):
                assert word not in outcome["replay_text"], (
                    f"The editor replay carries {word!r}, which the "
                    "LEGACY path delivered and no editor retract "
                    f"removed. Replay: {outcome['replay_text']!r}"
                )
        finally:
            await proc.stop()

    @pytest.mark.asyncio
    async def test_the_legacy_replay_omits_words_the_editor_delivered(
        self, stand,
    ):
        """Case B of .3.1.10, the fired-mark spelling, one earlier word.

        The mark fires while the policy redirects, so "?" and the
        earlier word "question" land in the EDITOR document. A new
        utterance then resets ``_used_editor_this_utterance`` at :1566
        and the policy has stopped redirecting, so that utterance's
        correction takes the LEGACY arm. The Input retract removes only
        what the Input side typed, so the restore must not add
        "question" -- the editor copy is untouched and still on screen.
        """
        proc, app, controller, window, policy = stand
        await proc.start()
        try:
            policy.redirect = True
            await _hold_across_the_boundary(proc, app, ["question"], "mark")

            assert proc._earlier_utterance_words_in_retract_span == [
                "question"
            ], (
                "The delivery did not confirm the earlier word, so "
                "there is no record for a restore to decide about."
            )
            assert "?" in window._text_edit.toPlainText(), (
                "The mark did not reach the editor, so this is not the "
                f"case under test. Document: "
                f"{window._text_edit.toPlainText()!r}"
            )
            before = list(app.legacy_texts())

            # A new utterance. Its start word resets the editor
            # stickiness at :1566, and the policy no longer redirects,
            # so this utterance is a legacy one throughout.
            policy.redirect = False
            await _word(proc, "alpha", 3, start=True, generation=2)
            assert not proc._used_editor_this_utterance, (
                "The new utterance is still routed to the editor, so "
                "the correction would take the editor arm and this "
                "test would measure the wrong decision."
            )
            assert (
                proc._earlier_utterance_words_generation
                == app.utterance_start_generation
            ), (
                "The record's generation no longer equals the count, so "
                "the legacy arm would drop the record for a reason that "
                "has nothing to do with the surface."
            )

            await _retraction(proc, 3, "beta")
            await asyncio.sleep(0.2)

            added = app.legacy_texts()[len(before):]
            assert added, "The legacy path sent nothing after the retract."
            assert "question" not in " ".join(added), (
                "The legacy restore typed an earlier word the EDITOR "
                "delivered. The Input retract removed only Input text, "
                "so the editor copy is still on screen and this is a "
                f"second one. Legacy sends after the retract: {added!r}"
            )
            # The held word became the mark, so what stays on the
            # editor screen is "?" rather than the word itself. That is
            # what makes the legacy restore wrong: the user can still
            # see everything those held words produced, and the legacy
            # retract removed none of it.
            assert "?" in window._text_edit.toPlainText(), (
                "The legacy retract emptied the editor, which would "
                "make the legacy restore correct and this test "
                f"meaningless. Document: "
                f"{window._text_edit.toPlainText()!r}"
            )
        finally:
            await proc.stop()

    @pytest.mark.asyncio
    async def test_the_legacy_replay_omits_a_dictated_word_the_editor_took(
        self, stand,
    ):
        """The literal sibling of the case above.

        The acceptance criteria ask for both spellings. Here the held
        words never become a mark: the hold expires and they are
        flushed as ordinary dictation, which promotes the record
        through ``_flush_pending_replacement_prefix_as_dictation``
        rather than through ``_record_replacement_delivery``. Both
        families converge on the same confirmation function, so both
        need the surface.
        """
        proc, app, controller, window, policy = stand
        await proc.start()
        try:
            policy.redirect = True
            await _start_utterance(app, 1)
            await _word(proc, "question", 1, start=True, generation=1)
            await _utterance_end(proc, 1)
            await _start_utterance(app, 2)
            # A word that cannot complete a punctuation name, so the
            # hold expires and the earlier word is dictated as itself.
            await _word(proc, "hello", 2, start=True, generation=2)
            await asyncio.sleep(0.6)

            assert proc._earlier_utterance_words_in_retract_span == [
                "question"
            ], (
                "The literal flush did not confirm the earlier word, so "
                "this spelling is not under test. Record: "
                f"{proc._earlier_utterance_words_in_retract_span!r}"
            )
            assert "question" in window._text_edit.toPlainText(), (
                "The flush did not reach the editor, so this is not the "
                f"case under test. Document: "
                f"{window._text_edit.toPlainText()!r}"
            )
            before = list(app.legacy_texts())

            policy.redirect = False
            await _word(proc, "alpha", 3, start=True, generation=2)
            assert not proc._used_editor_this_utterance, (
                "The new utterance is still routed to the editor."
            )

            await _retraction(proc, 3, "beta")
            await asyncio.sleep(0.2)

            added = app.legacy_texts()[len(before):]
            assert added, "The legacy path sent nothing after the retract."
            assert "question" not in " ".join(added), (
                "The legacy restore typed an earlier word the EDITOR "
                "delivered, so the user now has two copies. "
                f"Legacy sends after the retract: {added!r}"
            )
        finally:
            await proc.stop()
