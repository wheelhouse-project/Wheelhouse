"""wh-user-patterns-split-bulletproof.5.2: a live wake-word change must not
rewrite an in-flight utterance's dictation fallback.

_resolve_finalization reconstructs the spoken prefix as
``f"{hotword} {buffer_text}"`` when ``hotword_active`` is True (router.py). The
live wake-word swap (SpeechProcessor.apply_hotword -> router.hotword) and the
word-processing loop both run in the same asyncio event loop, and the loop
awaits the word queue between words -- so a ``pm_set_hotword`` GUI action can
land between the fresh-hotword detection and the buffer's timeout
finalization. Reading the *current* ``router.hotword`` at finalization would
then reconstruct the prefix from the NEW wake word, inserting a word the user
never spoke ("computer delete ..." for a spoken "x-ray delete ...").

Fix: the router snapshots the wake word at fresh-hotword detection and uses
that snapshot for the reconstruction, so a mid-utterance swap cannot rewrite
an already-started utterance.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.router import SpeechRouter
from speech.pattern_catalog import PatternCatalog
from speech.word_event import WordEvent
from speech.domain import ProcessingMode, Action


@pytest.fixture
def catalog():
    return PatternCatalog("speech/config/patterns.toml")


@pytest.fixture
def router(catalog):
    return SpeechRouter(catalog, hotword="x-ray")


def _detect_hotword(router, word="x-ray"):
    """Drive the fresh-hotword detection path so the router snapshots it."""
    ev = WordEvent(word=word, start_of_utterance=True, end_of_utterance=False)
    decision = router.decide(ev, ProcessingMode.IDLE, [], {})
    assert decision.action == Action.TRANSITION
    assert decision.target_mode == ProcessingMode.HOTWORD_BUFFERING
    return decision


def test_fallback_uses_hotword_at_detection_not_current(router):
    # User spoke the old wake word "x-ray" ...
    _detect_hotword(router, "x-ray")
    # ... then a live wake-word swap lands mid-utterance (as apply_hotword does
    # by overwriting router.hotword) ...
    router.hotword = "computer"
    # ... and the buffered words never match a command, so the buffer times out
    # to dictation.
    decision = router.decide_timeout(["hello", "world"], hotword_active=True)
    assert decision.action == Action.DICTATE
    # The reconstructed prefix must be the spoken "x-ray", not "computer".
    assert decision.payload == "x-ray hello world"


def test_fallback_without_prior_detection_uses_current_hotword(router):
    # Defensive: if hotword_active is somehow True without a fresh detection
    # having captured a snapshot, the reconstruction still falls back to the
    # live hotword rather than crashing or emitting an empty prefix.
    decision = router.decide_timeout(["hello", "world"], hotword_active=True)
    assert decision.action == Action.DICTATE
    assert decision.payload == "x-ray hello world"


def test_new_hotword_applies_to_next_utterance(router):
    # After a swap, a fresh detection of the NEW wake word snapshots it, so the
    # next utterance reconstructs from the new word -- the swap is not deferred.
    router.hotword = "computer"
    _detect_hotword(router, "computer")
    decision = router.decide_timeout(["hello", "world"], hotword_active=True)
    assert decision.payload == "computer hello world"
