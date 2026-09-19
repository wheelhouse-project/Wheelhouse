"""The shipped scroll patterns (wh-voice-access-parity.2.3, part one).

Four spoken commands -- "scroll up", "scroll down", "scroll left", "scroll
right", each with an optional spoken number -- against the REAL shipped
services/wheelhouse/speech/config/patterns.toml, not a fixture. A test written
against a hand-built file would pass while the shipped file said something
else.

What these pin down:

* Each spoken form resolves to the intended entry under first-match-wins over
  the whole file, so no earlier pattern shadows a scroll command.
* Every scroll entry carries ``whole_utterance_only = true``. "scroll" is an
  ordinary English word: without the flag, "scroll down to the bottom of the
  page" would run the command as soon as the word "down" arrived and drop the
  rest of the sentence. That is the same defect codex reported for the
  show/hide entries as wh-voice-access-parity.1.6.2.1.
* Each entry calls the ``scroll`` action with its own direction and passes the
  optional count group through as the last argument.
* The count group is a REAL capture group whose whole body is ``\\d+``, which
  is what the pattern lexer requires of a numeric argument.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from speech.pattern_catalog import PatternCatalog

PATTERNS_PATH = Path(__file__).parent.parent / "speech" / "config" / "patterns.toml"

# Spoken form -> the doc_id that must answer it.
SCROLL_FORMS = {
    "scroll up": "scroll-up",
    "scroll down": "scroll-down",
    "scroll left": "scroll-left",
    "scroll right": "scroll-right",
    "scroll up 3": "scroll-up",
    "scroll down 10": "scroll-down",
    "scroll left 2": "scroll-left",
    "scroll right 5": "scroll-right",
}

SCROLL_DOC_IDS = ("scroll-up", "scroll-down", "scroll-left", "scroll-right")


@pytest.fixture(scope="module")
def shipped_entries() -> list[dict]:
    """Every [[pattern]] block of the shipped file, in file order."""
    with PATTERNS_PATH.open("rb") as handle:
        return tomllib.load(handle)["pattern"]


@pytest.fixture(scope="module")
def catalog() -> PatternCatalog:
    return PatternCatalog(str(PATTERNS_PATH))


def _entry(shipped_entries: list[dict], doc_id: str) -> dict:
    matches = [e for e in shipped_entries if e.get("doc_id") == doc_id]
    assert len(matches) == 1, f"expected exactly one {doc_id!r} entry, got {len(matches)}"
    return matches[0]


def _first_match_raw_pattern(catalog: PatternCatalog, phrase: str) -> str | None:
    """The raw pattern of the first entry in FILE ORDER that matches."""
    for entry in catalog.get_all_patterns():
        if entry["compiled_pattern"].match(phrase):
            return entry["raw_pattern"]
    return None


class TestScrollEntriesExist:
    @pytest.mark.parametrize("doc_id", SCROLL_DOC_IDS)
    def test_each_direction_has_exactly_one_entry(self, shipped_entries, doc_id):
        _entry(shipped_entries, doc_id)


class TestScrollFirstMatchOrder:
    @pytest.mark.parametrize("phrase,doc_id", sorted(SCROLL_FORMS.items()))
    def test_the_spoken_form_reaches_its_own_entry(
        self, catalog, shipped_entries, phrase, doc_id
    ):
        expected = _entry(shipped_entries, doc_id)["pattern"]
        assert _first_match_raw_pattern(catalog, phrase) == expected


class TestScrollFlagsAndActions:
    @pytest.mark.parametrize("doc_id", SCROLL_DOC_IDS)
    def test_the_entry_is_whole_utterance_only(self, shipped_entries, doc_id):
        """"scroll" is ordinary English; the command must not fire mid-sentence."""
        assert _entry(shipped_entries, doc_id).get("whole_utterance_only") is True

    @pytest.mark.parametrize("doc_id", SCROLL_DOC_IDS)
    def test_the_entry_needs_no_hotword(self, shipped_entries, doc_id):
        """Voice Access scrolls with no prefix, and the flag above is the guard."""
        assert _entry(shipped_entries, doc_id).get("requires_hotword", False) is False

    @pytest.mark.parametrize("doc_id", SCROLL_DOC_IDS)
    def test_the_entry_calls_the_scroll_action_with_its_direction(
        self, shipped_entries, doc_id
    ):
        entry = _entry(shipped_entries, doc_id)
        assert len(entry["actions"]) == 1
        action = entry["actions"][0]
        assert action["function"] == "scroll"
        assert action["params"] == [doc_id.split("-")[1], "g1"]

    @pytest.mark.parametrize("doc_id", SCROLL_DOC_IDS)
    def test_the_count_group_is_a_real_digits_only_group(
        self, shipped_entries, doc_id
    ):
        """The lexer requires a real capture group whose whole body is \\d+."""
        assert r"(\d+)?" in _entry(shipped_entries, doc_id)["pattern"]


class TestScrollDoesNotShadowDictation:
    @pytest.mark.parametrize(
        "phrase",
        [
            "scroll",
            "scroll the list",
            "scroll down to the bottom",
            "scroll up the page",
        ],
    )
    def test_a_longer_phrase_does_not_reach_a_scroll_entry(self, catalog, phrase):
        """Only the exact command matches; everything longer stays dictation.

        The whole_utterance_only flag is what stops the router executing early,
        but the anchored pattern is the first line of defence: a longer phrase
        must not match the entry at all.
        """
        matched = _first_match_raw_pattern(catalog, phrase)
        assert matched is None or "scroll" not in matched
