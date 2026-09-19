"""The shipped continuous-scroll patterns (wh-voice-access-parity.2.3.3).

Five spoken commands against the REAL shipped
services/wheelhouse/speech/config/patterns.toml: "start scrolling up",
"start scrolling down", "start scrolling left", "start scrolling right", and
"stop scrolling". Part one of this work shipped the four discrete scroll
commands; this is part two, the scroll that keeps going until a stop word
arrives.

What these pin down:

* Each spoken form resolves to its own entry under first-match-wins over the
  whole file, so no earlier pattern shadows a continuous-scroll command.
* Every entry carries ``whole_utterance_only = true``. "scrolling" and "stop"
  are ordinary English words. Without the flag the router would run the
  command the moment the matching word arrived, so "stop scrolling through
  the list and read it to me" would stop the scroll and drop the rest of the
  sentence. This is the same reasoning the discrete four record
  (patterns.toml, the comment above the scrolling section).
* Each start entry calls ``start_continuous_scroll`` with its own direction,
  and the stop entry calls ``stop_continuous_scroll`` with no arguments.
* Dictated prose holding "scrolling" or "stop" inside a longer sentence still
  reaches no continuous-scroll entry, which is what keeps it typed as words.

WHY FOUR START ENTRIES AND NOT ONE WITH A DIRECTION GROUP. The four discrete
scroll entries are four separate anchored patterns for the same reason: the
help document build refuses an alias group whose members differ in their
actions, and each direction passes a different argument. Four entries also
keep the direction a literal in the shipped file, where a reader can see it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from speech.pattern_catalog import PatternCatalog

PATTERNS_PATH = Path(__file__).parent.parent / "speech" / "config" / "patterns.toml"

# Spoken form -> the doc_id that must answer it.
CONTINUOUS_FORMS = {
    "start scrolling up": "scroll-start-up",
    "start scrolling down": "scroll-start-down",
    "start scrolling left": "scroll-start-left",
    "start scrolling right": "scroll-start-right",
    "stop scrolling": "scroll-stop",
}

START_DOC_IDS = (
    "scroll-start-up",
    "scroll-start-down",
    "scroll-start-left",
    "scroll-start-right",
)
ALL_DOC_IDS = START_DOC_IDS + ("scroll-stop",)


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
    assert len(matches) == 1, (
        f"expected exactly one {doc_id!r} entry, got {len(matches)}"
    )
    return matches[0]


def _first_match_raw_pattern(catalog: PatternCatalog, phrase: str) -> str | None:
    """The raw pattern of the first entry in FILE ORDER that matches."""
    for entry in catalog.get_all_patterns():
        if entry["compiled_pattern"].match(phrase):
            return entry["raw_pattern"]
    return None


class TestContinuousScrollEntriesExist:
    @pytest.mark.parametrize("doc_id", ALL_DOC_IDS)
    def test_each_command_has_exactly_one_entry(self, shipped_entries, doc_id):
        _entry(shipped_entries, doc_id)


class TestContinuousScrollFirstMatchOrder:
    @pytest.mark.parametrize("phrase,doc_id", sorted(CONTINUOUS_FORMS.items()))
    def test_the_spoken_form_reaches_its_own_entry(
        self, catalog, shipped_entries, phrase, doc_id
    ):
        expected = _entry(shipped_entries, doc_id)["pattern"]
        assert _first_match_raw_pattern(catalog, phrase) == expected


class TestContinuousScrollFlagsAndActions:
    @pytest.mark.parametrize("doc_id", ALL_DOC_IDS)
    def test_the_entry_is_whole_utterance_only(self, shipped_entries, doc_id):
        """"scrolling" and "stop" are ordinary English words."""
        assert _entry(shipped_entries, doc_id).get("whole_utterance_only") is True

    @pytest.mark.parametrize("doc_id", ALL_DOC_IDS)
    def test_the_entry_needs_no_hotword(self, shipped_entries, doc_id):
        """Voice Access scrolls with no prefix; the flag above is the guard."""
        assert _entry(shipped_entries, doc_id).get("requires_hotword", False) is False

    @pytest.mark.parametrize("doc_id", START_DOC_IDS)
    def test_each_start_entry_passes_its_own_direction(
        self, shipped_entries, doc_id
    ):
        entry = _entry(shipped_entries, doc_id)
        assert len(entry["actions"]) == 1
        action = entry["actions"][0]
        assert action["function"] == "start_continuous_scroll"
        assert action["params"] == [doc_id.rsplit("-", 1)[1]]

    def test_the_stop_entry_takes_no_arguments(self, shipped_entries):
        entry = _entry(shipped_entries, "scroll-stop")
        assert len(entry["actions"]) == 1
        action = entry["actions"][0]
        assert action["function"] == "stop_continuous_scroll"
        assert action.get("params", []) == []


class TestContinuousScrollDoesNotShadowDictation:
    """Acceptance item 6: prose holding these words is still typed.

    Every phrase here is a sentence a person could dictate. None of them may
    reach a continuous-scroll entry. The anchored pattern is the first guard
    and ``whole_utterance_only`` is the second; this test measures the first.
    """

    @pytest.mark.parametrize(
        "phrase",
        [
            "stop",
            "please stop scrolling the page",
            "stop scrolling and read it to me",
            "start scrolling when you reach the end",
            "I will start scrolling down the list",
            "the scrolling stopped",
            "stop scrolling up there",
        ],
    )
    def test_a_longer_phrase_does_not_reach_a_continuous_entry(
        self, catalog, shipped_entries, phrase
    ):
        matched = _first_match_raw_pattern(catalog, phrase)
        continuous_patterns = {
            _entry(shipped_entries, doc_id)["pattern"] for doc_id in ALL_DOC_IDS
        }
        assert matched not in continuous_patterns, (
            f"{phrase!r} reached a continuous-scroll command; a sentence a "
            f"person dictates must be typed as words"
        )
