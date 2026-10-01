""""clicks" is a second spoken form of "click" in the click-element command.

wh-clicks-spoken-click. Parakeet v3 on CPU heard "x-ray click show
recently used files" as the partial text 'x-ray click show recently used'
and then revised it to the final text 'x-ray clicks show recently used
files' (wheelhouse.log, UTT-48 and UTT-52, 2026-09-27). The replay after
the revision found no command that starts with "clicks", so Wheelhouse
typed the words. David chose (QUESTIONS-2026-09-27.md item 4) to accept
"clicks" as a second spoken form of "click" in the click-element row
only. The hotword requirement was dropped on 2026-09-30
(wh-safety-word-free-commands).

The stack tests use the REAL catalog, router, parser and actions (the
helpers from tests/test_grid_number_badge_click.py); only the logic
controller at the process boundary is faked.
"""
from __future__ import annotations

import asyncio
import re

import pytest

# The helper module puts the repository root on sys.path, so it is
# imported before the package-qualified OverlayState it re-exports.
from tests.test_grid_number_badge_click import (
    PATTERNS,
    OverlayState,
    _end_marker,
    _make_stack,
    _speak,
    _word,
)
from speech.pattern_catalog import PatternCatalog
from speech.word_event import WordEvent


TARGET = "show recently used files"


def _click_names(lc):
    return [query.name for query in lc.clicks]


@pytest.mark.parametrize("verb", ["click", "clicks", "tap"])
def test_verb_after_hotword_clicks_the_named_control(verb):
    # A1: "clicks" runs click_element with the same target text as "click".
    processor, app, lc = _make_stack(OverlayState.CLOSED)
    _speak(processor, f"x-ray {verb} {TARGET}")

    assert app.inserted_texts() == []
    assert _click_names(lc) == [TARGET]


def test_clicks_query_equals_the_click_query():
    # A1: the whole query, not only its name, is the one "click" builds.
    queries = {}
    for verb in ("click", "clicks"):
        processor, app, lc = _make_stack(OverlayState.CLOSED)
        _speak(processor, f"x-ray {verb} submit button")
        assert app.inserted_texts() == []
        assert len(lc.clicks) == 1
        queries[verb] = lc.clicks[0]
    assert queries["clicks"] == queries["click"]


def test_revision_from_click_to_clicks_replays_as_a_click():
    # A1, the path in the log: the partial text ends before "files", the
    # final text revises "click" to "clicks", a retraction is queued and
    # the corrected final replays. The replay must click, and type nothing.
    processor, app, lc = _make_stack(OverlayState.CLOSED)

    async def _run():
        partial = "x-ray click show recently used".split()
        for index, one in enumerate(partial):
            await processor.process_word_event(_word(one, start=(index == 0)))
        await processor.process_word_event(
            WordEvent(
                word="",
                start_of_utterance=False,
                end_of_utterance=False,
                utterance_id=1,
                is_retraction_marker=True,
                retraction_full_text=f"x-ray clicks {TARGET}",
            )
        )
        await processor.process_word_event(_end_marker())

    asyncio.run(_run())

    assert app.inserted_texts() == []
    assert _click_names(lc) == [TARGET]


@pytest.mark.parametrize("verb", ["click", "clicks", "tap"])
def test_verb_without_hotword_clicks_the_named_control(verb):
    # A2, changed by wh-safety-word-free-commands: the click row no longer
    # needs the safety word, so "clicks" and "click" both run it unspoken.
    processor, app, lc = _make_stack(OverlayState.CLOSED)
    _speak(processor, f"{verb} {TARGET}")

    assert app.inserted_texts() == []
    assert _click_names(lc) == [TARGET]


# A3: the first shipped row that matches each utterance. Only the
# click-element row gains "clicks"; every other click-related row answers
# exactly as before.
FIRST_MATCH = [
    ("click show recently used files", "click-element"),
    ("tap show recently used files", "click-element"),
    ("clicks show recently used files", "click-element"),
    ("click", "grid-click"),
    ("tap", "grid-click"),
    ("right click", "grid-gesture-click"),
    ("double click", "grid-gesture-click"),
    ("triple click", "grid-gesture-click"),
    ("right click recycle bin", "click-element-gesture"),
    ("double click recycle bin", "click-element-gesture"),
    ("click to talk mode", "click-to-talk-mode"),
]


def _first_match_doc_id(catalog, utterance):
    for entry in catalog.get_all_patterns():
        if entry["compiled_pattern"].match(utterance):
            return entry.get("doc_id")
    return None


# Explicit ids with hyphens: an id with a space cannot be matched by name
# in tests/mutation_gate_clicks_spoken_click.py.
@pytest.mark.parametrize(
    ("utterance", "doc_id"),
    [pytest.param(u, d, id=u.replace(" ", "-")) for u, d in FIRST_MATCH],
)
def test_first_matching_row_is_unchanged_except_for_clicks(utterance, doc_id):
    catalog = PatternCatalog(str(PATTERNS))
    assert _first_match_doc_id(catalog, utterance) == doc_id


def test_bare_clicks_matches_no_row():
    # "clicks" alone is not a grid click; only the click-element row
    # changed, and it needs a control name after the word.
    catalog = PatternCatalog(str(PATTERNS))
    assert _first_match_doc_id(catalog, "clicks") is None


def test_pattern_manager_shows_click_clicks_and_tap():
    # The row is spelled with "clicks" as its own alternative, not as
    # "clicks?": the Pattern Manager display drops the "?" and would then
    # name "clicks" and "tap" but never "click".
    from speech.pattern_manager import PatternManager

    catalog = PatternCatalog(str(PATTERNS))
    [row] = [
        entry for entry in catalog.get_raw_system_entries()
        if entry.get("doc_id") == "click-element"
    ]
    assert PatternManager._trigger_display(row["pattern"]) == (
        "click (or clicks, tap)"
    )


def test_click_element_row_is_the_new_text():
    # A4: every text that states the trigger quotes this exact expression.
    catalog = PatternCatalog(str(PATTERNS))
    rows = [
        entry for entry in catalog.get_raw_system_entries()
        if entry.get("doc_id") == "click-element"
    ]
    assert len(rows) == 1
    assert rows[0]["pattern"] == r"^(?:click|clicks|tap)\s+(.+)$"
    # wh-safety-word-free-commands: the row runs without the safety word.
    assert not rows[0].get("requires_hotword")
    assert re.fullmatch(rows[0]["pattern"], "clicks x")
