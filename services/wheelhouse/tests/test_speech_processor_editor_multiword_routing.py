"""Multi-word editor routing + retraction accounting (wh-editor-retract-dup).

Root cause this module locks down:

The persistent-editor cut-over (wh-g2-refactor.18 slice 18.32.1) re-consults
``focus_redirect_policy.should_redirect`` for EVERY dictated word. That call
is an "open the editor" decision, not a "route this word" decision. Once the
first streamed word of an utterance opens the editor, the LogicMirror reports
an OPEN state (reason ``editor_already_open``) and/or the foreground HWND is
now the editor window (reason ``not_a_terminal``), so words 2..N decline and
fall through to the legacy ``intelligent_insert_text`` path. Those words never
reach the CreditLedger.

At a Google STT MODE3 retraction the speech processor asks the editor to
retract ``_editor_chars_this_utterance`` -- which only ever accumulated the
FIRST word's length -- and replays the full final text. The untracked words
2..N remain in the document, so the final text is duplicated.

The contract these tests pin:

  1. Editor routing is STICKY per utterance. Once a word of the current
     utterance has been routed to the editor, every subsequent dictation word
     of the same utterance routes to ``insert_editor_word`` WITHOUT
     re-consulting ``should_redirect``.
  2. ``_editor_chars_this_utterance`` accumulates the char count the editor
     actually inserted (the ``insert_editor_word`` return value), not
     ``len(text)`` -- so editor-side spacing/perfection is accounted for.
  3. A retraction after a multi-word editor utterance requests the FULL
     accumulated count and replays the final text exactly once.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

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


class _MockApp:
    def __init__(self) -> None:
        self.actions: List[Dict[str, Any]] = []

    async def send_command(self, payload: dict) -> None:
        self.actions.append({"kind": "command", **payload})

    async def send_request(self, action: str, params: dict) -> dict:
        self.actions.append(
            {"kind": "request", "action": action, "params": params}
        )
        return {"status": "success"}


class _ScriptedPolicy:
    """Policy stub that returns scripted ``should_redirect`` decisions.

    Models production: the first call (first word) opens the editor; any
    later call would decline (``editor_already_open``). The fix must mean
    later words never reach ``should_redirect`` at all, which the
    ``calls`` counter verifies.
    """

    def __init__(self, first_open: bool) -> None:
        self.calls = 0
        self._first_open = first_open

    async def should_redirect(self, focused_hwnd: int) -> RedirectDecision:
        self.calls += 1
        if self.calls == 1 and self._first_open:
            return RedirectDecision(
                open_editor=True,
                target_terminal_hwnd=focused_hwnd,
                reason="terminal_at_prompt",
            )
        return RedirectDecision(
            open_editor=False,
            target_terminal_hwnd=0,
            reason="editor_already_open",
        )

    def on_utterance_end(self) -> None:
        return None


def _build_processor(
    *,
    first_open: bool,
    insert_return,
) -> tuple[SpeechProcessor, _MockApp, MagicMock, _ScriptedPolicy]:
    policy = _ScriptedPolicy(first_open=first_open)
    catalog = PatternCatalog(str(patterns_path))
    app = _MockApp()
    text_parser = TextParser(MagicMock(app=app), catalog)
    logic_controller = MagicMock()
    logic_controller.insert_editor_word = AsyncMock(side_effect=insert_return)
    logic_controller.retract_editor_text = AsyncMock()
    logic_controller.show_editor_persistent = MagicMock()

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
    return proc, app, logic_controller, policy


async def _dictate(proc: SpeechProcessor, words: list[str], utterance_id: int):
    for i, w in enumerate(words):
        await proc.process_word_event(
            WordEvent(
                word=w,
                start_of_utterance=(i == 0),
                end_of_utterance=False,
                utterance_id=utterance_id,
            )
        )


@pytest.mark.asyncio
async def test_all_words_of_editor_utterance_route_to_ledger():
    """Every word of an editor utterance routes to insert_editor_word.

    Words 2..N must NOT fall through to the legacy intelligent_insert_text
    path even though should_redirect would now decline (editor open).
    """
    # Each insert reports the perfected char count (word + a leading space
    # for words after the first); the exact values are exercised in the
    # accounting test below.
    proc, app, lc, policy = _build_processor(
        first_open=True,
        insert_return=lambda text, uid: len(text),
    )

    await _dictate(proc, ["but", "lets", "talk"], utterance_id=66)

    assert lc.insert_editor_word.await_count == 3, (
        "all three words must reach the editor ledger; got "
        f"{lc.insert_editor_word.await_count}"
    )
    legacy = [
        a for a in app.actions if a.get("action") == "intelligent_insert_text"
    ]
    assert legacy == [], f"no legacy dictation expected, got {legacy}"
    # Sticky: the policy is consulted only for the first word.
    assert policy.calls == 1, (
        "should_redirect must be consulted once per utterance, not per word; "
        f"got {policy.calls}"
    )


@pytest.mark.asyncio
async def test_editor_accounting_accumulates_reported_chars():
    """_editor_chars_this_utterance sums the editor-reported insert counts.

    The editor applies TextPerfector spacing, so word 2+ inserts a leading
    space. The accounting must follow the returned char count, not len(text),
    or the retract request will be short by one char per inter-word space.
    """
    # "but" -> 3, " lets" -> 5, " talk" -> 5 (leading space included).
    returns = iter([3, 5, 5])
    proc, app, lc, policy = _build_processor(
        first_open=True,
        insert_return=lambda text, uid: next(returns),
    )

    await _dictate(proc, ["but", "lets", "talk"], utterance_id=66)

    assert proc._editor_chars_this_utterance == 13, (
        "accounting must sum reported chars (3+5+5); got "
        f"{proc._editor_chars_this_utterance}"
    )


@pytest.mark.asyncio
async def test_retraction_requests_full_accumulated_count():
    """A MODE3 retraction retracts the whole accumulated span and replays once.

    This is the duplication guard: before the fix the retract requested only
    the first word's length, leaving words 2..N in the document under the
    replayed final text.
    """
    returns = iter([3, 5, 5])
    proc, app, lc, policy = _build_processor(
        first_open=True,
        insert_return=lambda text, uid: next(returns),
    )

    await _dictate(proc, ["but", "lets", "talk"], utterance_id=66)

    final = "but lets talk about phase one"
    await proc.process_word_event(
        WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=66,
            is_retraction_marker=True,
            retraction_full_text=final,
        )
    )

    assert lc.retract_editor_text.await_count == 1
    _, kwargs = lc.retract_editor_text.await_args
    assert kwargs["chars_requested"] == 13, (
        "retract must request the full accumulated span (13); got "
        f"{kwargs['chars_requested']}"
    )
    assert kwargs["replay_text"] == final
    assert kwargs["utterance_id"] == "66"


@pytest.mark.asyncio
async def test_sticky_routing_survives_first_insert_raise():
    """A raised first editor insert must not split the utterance.

    Finding wh-editor-retract-dup.2.1. ``show_editor_persistent`` has
    already opened the editor and given it foreground, so the utterance is
    committed to the editor route. If ``insert_editor_word`` then raises on
    the first word, ``_used_editor_this_utterance`` must still flip True so
    words 2..N stay sticky to the editor. Otherwise word 2 re-consults
    ``should_redirect`` (now declining, editor already open) and falls
    through to the legacy path -- exactly the split-utterance bug the fix
    exists to kill.
    """
    raised: list[bool] = []

    def _insert(text, uid):
        if not raised:
            raised.append(True)
            raise RuntimeError("simulated editor IPC failure on first word")
        return len(text)

    proc, app, lc, policy = _build_processor(
        first_open=True,
        insert_return=_insert,
    )

    await _dictate(proc, ["but", "lets", "talk"], utterance_id=66)

    assert lc.insert_editor_word.await_count == 3, (
        "all three words must be attempted against the editor even after "
        f"the first insert raised; got {lc.insert_editor_word.await_count}"
    )
    legacy = [
        a for a in app.actions if a.get("action") == "intelligent_insert_text"
    ]
    assert legacy == [], (
        "no word may fall through to the legacy path after the editor "
        f"route is committed; got {legacy}"
    )
    assert policy.calls == 1, (
        "should_redirect must be consulted once even when the first insert "
        f"raised; got {policy.calls}"
    )


@pytest.mark.asyncio
async def test_retract_reset_counts_canonical_crlf():
    """The post-retract reset must count the canonical replay text.

    Finding wh-editor-retract-dup.2.2. The ledger canonicalises CRLF to a
    single LF before recording the replay run's cluster count, so a CRLF in
    the replay text counts as ONE cluster on the ledger side. The
    speech-side reset must use the same canonical count; counting the raw
    text treats CRLF as two clusters and over-counts the span a chained
    retract will request.
    """
    from shared.grapheme import count_grapheme_clusters  # noqa: E402

    returns = iter([3, 5, 5])
    proc, app, lc, policy = _build_processor(
        first_open=True,
        insert_return=lambda text, uid: next(returns),
    )

    await _dictate(proc, ["but", "lets", "talk"], utterance_id=66)

    final = "line one\r\nline two"
    await proc.process_word_event(
        WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=66,
            is_retraction_marker=True,
            retraction_full_text=final,
        )
    )

    canonical = final.replace("\r\n", "\n").replace("\r", "\n")
    assert proc._editor_chars_this_utterance == count_grapheme_clusters(
        canonical,
    ), (
        "reset must count the canonical (CRLF->LF) replay text; got "
        f"{proc._editor_chars_this_utterance}"
    )
    # The raw count over-counts the CRLF as two clusters; the reset must
    # NOT use it.
    assert proc._editor_chars_this_utterance == (
        count_grapheme_clusters(final) - 1
    )


@pytest.mark.asyncio
async def test_chained_retraction_requests_canonical_count():
    """A second retraction after CRLF replay requests the canonical span.

    Finding wh-editor-retract-dup.2.2. After the first retract resets the
    accounting to the replay span, a chained MODE3 retraction must request
    the canonical cluster count the ledger stored, not the raw over-count
    that would trip ledger_underrun and skip the replay.
    """
    from shared.grapheme import count_grapheme_clusters  # noqa: E402

    returns = iter([3, 5, 5])
    proc, app, lc, policy = _build_processor(
        first_open=True,
        insert_return=lambda text, uid: next(returns),
    )

    await _dictate(proc, ["but", "lets", "talk"], utterance_id=66)

    final_one = "line one\r\nline two"
    final_two = "line one\r\nline three"
    for final in (final_one, final_two):
        await proc.process_word_event(
            WordEvent(
                word="",
                start_of_utterance=False,
                end_of_utterance=False,
                utterance_id=66,
                is_retraction_marker=True,
                retraction_full_text=final,
            )
        )

    assert lc.retract_editor_text.await_count == 2
    _, kwargs2 = lc.retract_editor_text.await_args_list[1]
    canonical_one = final_one.replace("\r\n", "\n").replace("\r", "\n")
    assert kwargs2["chars_requested"] == count_grapheme_clusters(
        canonical_one,
    ), (
        "the chained retract must request the canonical cluster count of the "
        f"first replay; got {kwargs2['chars_requested']}"
    )


@pytest.mark.asyncio
async def test_retraction_is_ledger_authoritative_whole_utterance():
    """wh-editor-retract-ledger-authoritative: the MODE3 retract carries
    whole_utterance=True so the GUI peels ALL ledger runs. The mirror
    count still rides along as the advisory chars_requested."""
    returns = iter([3, 5, 5])
    proc, app, lc, policy = _build_processor(
        first_open=True,
        insert_return=lambda text, uid: next(returns),
    )

    await _dictate(proc, ["but", "lets", "talk"], utterance_id=66)

    final = "but lets talk"
    await proc.process_word_event(
        WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=66,
            is_retraction_marker=True,
            retraction_full_text=final,
        )
    )

    assert lc.retract_editor_text.await_count == 1
    _, kwargs = lc.retract_editor_text.await_args
    assert kwargs["whole_utterance"] is True
    assert kwargs["chars_requested"] == 13


@pytest.mark.asyncio
async def test_retraction_fires_even_when_mirror_is_zero():
    """The insert-timeout drift regression: every insert response timed
    out Logic-side (insert_editor_word returned 0), so the mirror reads
    0 -- but the words may have landed in the editor. The retract must
    still fire (whole-utterance mode) instead of silently skipping and
    leaving the stale text under the replay."""
    proc, app, lc, policy = _build_processor(
        first_open=True,
        insert_return=lambda text, uid: 0,
    )

    await _dictate(proc, ["but", "lets", "talk"], utterance_id=66)
    assert proc._editor_chars_this_utterance == 0

    final = "but lets talk"
    await proc.process_word_event(
        WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=66,
            is_retraction_marker=True,
            retraction_full_text=final,
        )
    )

    assert lc.retract_editor_text.await_count == 1, (
        "the retract must fire on the editor path even with a zero "
        "mirror; a counted-mode skip here leaves the timed-out words "
        "in the document under the replayed final"
    )
    _, kwargs = lc.retract_editor_text.await_args
    assert kwargs["whole_utterance"] is True
    assert kwargs["chars_requested"] == 0
    # No legacy retract fallback and no word-by-word replay: the GUI
    # handler replays inline on the editor path.
    legacy_retracts = [
        a for a in app.actions if a.get("action") == "retract"
    ]
    assert legacy_retracts == []


# wh-itn-percent-digits.1.1: the editor arm never runs the replacement
# patterns. The word-by-word replay (the legacy arm) reaches
# ``router.decide`` through ``process_word_event`` and so turns the word
# "percent" into "%"; the editor arm hands the corrected final to
# ``retract_editor_text`` as one string and the ledger inserts it as given.
# A final of "50 percent" therefore reached the editor document with its
# word "percent" still a word.
async def _correct_an_editor_utterance(
    proc, lc, words: list[str], final: str, utterance_id: int,
    earlier_words: list[str] | None = None,
):
    """Dictate ``words`` into the editor, correct them to ``final``.

    ``earlier_words`` is the record the processor keeps after an earlier
    utterance's words went into the editor. It is set AFTER the
    dictation because the first word of an utterance clears it (the
    stamped delivery path sets it in production).
    """
    await _dictate(proc, words, utterance_id=utterance_id)
    if earlier_words:
        proc._earlier_utterance_words_in_retract_span = list(earlier_words)
        proc._earlier_utterance_words_surface = "editor"
    await proc.process_word_event(
        WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=utterance_id,
            is_retraction_marker=True,
            retraction_full_text=final,
        )
    )
    assert lc.retract_editor_text.await_count == 1, (
        "the correction never reached the editor arm, so the replay "
        "text this test reads does not exist"
    )
    _, kwargs = lc.retract_editor_text.await_args
    return kwargs


@pytest.mark.asyncio
async def test_a_corrected_percent_phrase_replays_as_the_symbol():
    """The replay text carries "50%" and not "50 percent".

    The retract-and-replay is one call into the editor's ledger, which
    inserts the text exactly as given, so the replacement has to be
    applied to the text before it is handed over.
    """
    proc, app, lc, policy = _build_processor(
        first_open=True,
        insert_return=lambda text, uid: len(text),
    )

    kwargs = await _correct_an_editor_utterance(
        proc, lc, ["growth", "was", "fifty"],
        "growth was 50 percent", utterance_id=71,
    )

    assert kwargs["replay_text"] == "growth was 50%", (
        "the editor replay must carry the symbol the replacement "
        "pattern writes, with no word 'percent' left and no space "
        f"before the symbol. Replay text: {kwargs['replay_text']!r}"
    )


@pytest.mark.asyncio
async def test_the_percent_pass_leaves_the_earlier_utterance_words_alone():
    """The pass runs on the corrected final BEFORE the earlier words join.

    The earlier utterance's SPOKEN words are put in front of the replay
    by ``_restore_earlier_utterance_words``. If the pass ran over the
    joined string it would rewrite the earlier word "percent" as well,
    and the user would see "%" where the earlier utterance said the word.
    """
    proc, app, lc, policy = _build_processor(
        first_open=True,
        insert_return=lambda text, uid: len(text),
    )

    kwargs = await _correct_an_editor_utterance(
        proc, lc, ["growth", "was", "fifty"],
        "growth was 50 percent", utterance_id=72,
        earlier_words=["ten", "percent"],
    )

    assert kwargs["replay_text"] == "ten percent growth was 50%", (
        "the earlier utterance's words must come back exactly as "
        "spoken, and only the corrected final may be rewritten. "
        f"Replay text: {kwargs['replay_text']!r}"
    )


@pytest.mark.asyncio
async def test_one_editor_correction_writes_one_restore_log_line(caplog):
    """The arm calls the restore twice and must log it once.

    The editor arm calls ``_restore_earlier_utterance_words`` on the
    same input twice, once for the text it sends to the editor and once
    for the record of the spoken words. A reader of a pipeline log
    counts restores by these lines, so two lines would describe a
    correction that never happened.
    """
    proc, app, lc, policy = _build_processor(
        first_open=True,
        insert_return=lambda text, uid: len(text),
    )

    with caplog.at_level(logging.INFO, logger="wheelhouse.pipeline"):
        await _correct_an_editor_utterance(
            proc, lc, ["growth", "was", "fifty"],
            "growth was 50 percent", utterance_id=74,
            earlier_words=["ten", "percent"],
        )

    lines = [
        record.getMessage() for record in caplog.records
        if "RETRACT restoring earlier-utterance words" in record.getMessage()
    ]
    assert len(lines) == 1, (
        "one correction must write one restore line. Lines: "
        f"{lines!r}"
    )


@pytest.mark.asyncio
async def test_the_utterance_record_keeps_the_spoken_form_after_the_pass():
    """The editor text carries "%" and the word record keeps "percent".

    ``_payload_is_the_whole_utterance`` compares ``_words_this_utterance``
    token for token with a later spoken payload. The record is built
    from the corrected final as spoken, so a payload spoken as the words
    still matches while the editor holds the symbol.
    """
    proc, app, lc, policy = _build_processor(
        first_open=True,
        insert_return=lambda text, uid: len(text),
    )

    kwargs = await _correct_an_editor_utterance(
        proc, lc, ["growth", "was", "fifty"],
        "growth was 50 percent", utterance_id=73,
    )

    assert kwargs["replay_text"] == "growth was 50%", (
        "the editor text must carry the symbol, or this test does not "
        f"cover the pass. Replay text: {kwargs['replay_text']!r}"
    )
    assert proc._words_this_utterance == [
        "growth", "was", "50", "percent",
    ], (
        "the record of this utterance's words must keep the spoken "
        "word 'percent' and not the symbol the editor holds. Record: "
        f"{proc._words_this_utterance!r}"
    )
    assert proc._payload_is_the_whole_utterance("growth was 50 percent")
