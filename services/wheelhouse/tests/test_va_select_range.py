"""Speech pattern tests for bd bead wh-voice-access-parity.1.2 (Voice Access
select-by-range commands).

STAGED FRAGMENT -- not yet part of the test suite and not runnable to a pass
today. It exercises the range-select pattern blocks authored in
patterns-staging/wh-voice-access-parity.1.2.toml, which are NOT in
services/wheelhouse/speech/config/patterns.toml yet. Every test in
TestSelectRangeCommands and TestUnselectAndClearSelection below will FAIL
until the boss places those blocks into the live file, ABOVE the
select-phrase entry (see the ORDERING HAZARD comment at the top of the .toml
fragment -- ^select (.+)$ currently swallows every phrase covered here). Do
not edit patterns.toml from this file to make it pass; that placement
decision belongs to the boss, not to this bead's authoring step.

SCOPE covered here (bd show wh-voice-access-parity.1.2): select next,
previous and last <n> characters, words, lines and paragraphs; unselect
that; clear selection.

OUT OF SCOPE / duplicate check: select word, select line, select paragraph
and their this-word, this-line, this-paragraph aliases are also named in
the bead's SCOPE list, but they already exist in patterns.toml (doc_ids
select-word, select-line, select-paragraph, each already widened to
^select (?:this )?word$ / line / paragraph) from the in-progress bead
wh-voice-access-parity.1.6. Verified by
`grep -n "select (?:this )?word\\|select (?:this )?line\\|select (?:this )?paragraph"
services/wheelhouse/speech/config/patterns.toml` in this worktree, which
found all three already present at lines 442, 450 and 458. This file does
not re-test them; they belong to bead .1.6's own tests.
"""

import re
import sys
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

# patterns-staging/ sits directly under the worktree root -- three levels
# above this file. service_dir is added too because the service modules use
# bare intra-service imports (from utils..., from ai...) that resolve only
# with the service directory itself on sys.path (wh-z69w).
project_root = Path(__file__).resolve().parents[3]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
service_dir = project_root / "services" / "wheelhouse"
if str(service_dir) not in sys.path:
    sys.path.insert(1, str(service_dir))

from services.wheelhouse.speech.pattern_catalog import PatternCatalog
from services.wheelhouse.speech.command_engine import TextParser
from services.wheelhouse.speech.pattern_transform import transform_pattern

PATTERNS_FILE = service_dir / "speech" / "config" / "patterns.toml"


def _production_pattern_entries():
    """Load the pattern entries straight from the live TOML file.

    Independent of the PatternCatalog wrapper so a catalog-loading bug
    cannot mask a missing or misshapen entry.
    """
    with PATTERNS_FILE.open("rb") as handle:
        return tomllib.load(handle)["pattern"]


@pytest.fixture
def catalog():
    """Real pattern catalog with production patterns."""
    return PatternCatalog(str(PATTERNS_FILE))


@pytest.fixture
def mock_app():
    app = Mock()
    app.send_command = AsyncMock()
    app.send_request = AsyncMock(return_value={"status": "success"})
    return app


@pytest.fixture
def parser(catalog, mock_app):
    """TextParser wrapped so authorized_command=True.

    These tests simulate command-mode text that in production has already
    passed the router's hotword gate (wh-qj70s / wh-z69w).
    """
    mock_handler = Mock()
    mock_handler.app = mock_app
    p = TextParser(mock_handler, catalog)
    orig = p.parse_and_execute
    p.parse_and_execute = (
        lambda text, **kw: orig(text, **{"authorized_command": True, **kw})
    )
    return p


# ============================================================================
# select next/previous/last <n> characters/words/lines/paragraphs
# ============================================================================

# (spoken_phrase, doc_id, expected hk keys, expected repeat count)
#
# Each unit gets three phrasings per direction-pair -- bare (repeat defaults
# to 1 via words_to_int(None)), a digit count, and a spoken-word count -- to
# cover the full breadth of words_to_int (services/wheelhouse/speech/
# actions.py, which reads its words through parse_number_word: zero, then
# one through nine hundred ninety nine). "previous" and "last" are
# the same backward direction in Voice Access (verified against
# support.microsoft.com/en-us/accessibility/windows/voice-access/
# voice-access-command-list, fetched 2026-08-24: "Select previous word" /
# "Select last word" / "Select next word" is one direction pair, not three
# directions), so both wordings are exercised against the SAME doc_id.
SELECT_RANGE_FORMS = [
    # characters: shift+right / shift+left (no ctrl -- single character)
    ("select next character", "select-next-characters", ["shift", "right"], 1),
    ("select next 3 characters", "select-next-characters", ["shift", "right"], 3),
    ("select next three characters", "select-next-characters", ["shift", "right"], 3),
    ("select previous character", "select-previous-characters", ["shift", "left"], 1),
    ("select previous 4 characters", "select-previous-characters", ["shift", "left"], 4),
    ("select previous four characters", "select-previous-characters", ["shift", "left"], 4),
    ("select last character", "select-previous-characters", ["shift", "left"], 1),
    ("select last 4 characters", "select-previous-characters", ["shift", "left"], 4),
    ("select last four characters", "select-previous-characters", ["shift", "left"], 4),
    # words: shift+ctrl+right / shift+ctrl+left (word-boundary jump)
    ("select next word", "select-next-words", ["shift", "ctrl", "right"], 1),
    ("select next 3 words", "select-next-words", ["shift", "ctrl", "right"], 3),
    ("select next three words", "select-next-words", ["shift", "ctrl", "right"], 3),
    ("select previous word", "select-previous-words", ["shift", "ctrl", "left"], 1),
    ("select previous 2 words", "select-previous-words", ["shift", "ctrl", "left"], 2),
    ("select previous two words", "select-previous-words", ["shift", "ctrl", "left"], 2),
    ("select last word", "select-previous-words", ["shift", "ctrl", "left"], 1),
    ("select last 2 words", "select-previous-words", ["shift", "ctrl", "left"], 2),
    ("select last two words", "select-previous-words", ["shift", "ctrl", "left"], 2),
    # lines: shift+down / shift+up (bead MECHANISM example)
    ("select next line", "select-next-lines", ["shift", "down"], 1),
    ("select next 5 lines", "select-next-lines", ["shift", "down"], 5),
    ("select next five lines", "select-next-lines", ["shift", "down"], 5),
    ("select previous line", "select-previous-lines", ["shift", "up"], 1),
    ("select previous 2 lines", "select-previous-lines", ["shift", "up"], 2),
    ("select previous two lines", "select-previous-lines", ["shift", "up"], 2),
    ("select last line", "select-previous-lines", ["shift", "up"], 1),
    ("select last 2 lines", "select-previous-lines", ["shift", "up"], 2),
    ("select last two lines", "select-previous-lines", ["shift", "up"], 2),
    # paragraphs: shift+ctrl+down / shift+ctrl+up (paragraph jump)
    ("select next paragraph", "select-next-paragraphs", ["shift", "ctrl", "down"], 1),
    ("select next 2 paragraphs", "select-next-paragraphs", ["shift", "ctrl", "down"], 2),
    ("select next two paragraphs", "select-next-paragraphs", ["shift", "ctrl", "down"], 2),
    ("select previous paragraph", "select-previous-paragraphs", ["shift", "ctrl", "up"], 1),
    ("select previous 3 paragraphs", "select-previous-paragraphs", ["shift", "ctrl", "up"], 3),
    ("select previous three paragraphs", "select-previous-paragraphs", ["shift", "ctrl", "up"], 3),
    ("select last paragraph", "select-previous-paragraphs", ["shift", "ctrl", "up"], 1),
    ("select last 3 paragraphs", "select-previous-paragraphs", ["shift", "ctrl", "up"], 3),
    ("select last three paragraphs", "select-previous-paragraphs", ["shift", "ctrl", "up"], 3),
]


class TestSelectRangeCommands:
    """select next/previous/last <n> characters/words/lines/paragraphs.

    Bead wh-voice-access-parity.1.2 SCOPE and MECHANISM: hk with a shift
    combination and g1 as the repeat count.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("spoken_phrase", "doc_id", "keys", "repeat"), SELECT_RANGE_FORMS
    )
    async def test_range_form_fires_expected_hotkey(
        self, parser, mock_app, spoken_phrase, doc_id, keys, repeat
    ):
        assert await parser.parse_and_execute(spoken_phrase) is True
        mock_app.send_command.assert_called_once()
        payload = mock_app.send_command.call_args.args[0]
        assert payload == {
            "action": "hotkey_action",
            "params": {"keys": keys, "repeat": repeat},
        }

    @pytest.mark.parametrize(
        ("spoken_phrase", "expected_doc_id", "_keys", "_repeat"), SELECT_RANGE_FORMS
    )
    def test_first_file_order_match_is_the_range_entry(
        self, spoken_phrase, expected_doc_id, _keys, _repeat
    ):
        """Ordering hazard, recorded in the .toml fragment header: every one
        of these phrases also matches select-phrase's ^select (.+)$ pattern.
        Once the boss places these blocks ABOVE select-phrase, first-match-
        wins must resolve to the range entry, not select-phrase. Measured
        2026-08-24 against the current worktree's patterns.toml (before
        placement): all three of "select next three words", "select
        previous two lines" and "select last four characters" resolve to
        select-phrase today, confirming the boss's collision measurement.
        """
        entries = _production_pattern_entries()
        # The catalog widens every numeric capture (\d+) to (\w+) at load
        # time (speech/pattern_transform.py), which is what lets spoken
        # numbers like "three" match. Apply the same transform here so the
        # walk tests the regexes the engine actually runs, not the raw TOML.
        first_match = next(
            (
                entry
                for entry in entries
                if re.compile(
                    transform_pattern(entry["pattern"])[0], re.IGNORECASE
                ).search(spoken_phrase)
            ),
            None,
        )
        assert first_match is not None, f"No production pattern matches {spoken_phrase!r}"
        assert first_match.get("doc_id") == expected_doc_id, (
            f"{spoken_phrase!r} first matched {first_match.get('doc_id')!r}, "
            f"not {expected_doc_id!r} -- check placement relative to select-phrase"
        )

    def test_repeat_capture_groups_are_real_and_last_hk_argument(self):
        """Lexer rule (epic wh-voice-access-parity, 2026-08-17 comment, load
        bearing): the repeat count must come from a REAL capture group whose
        whole body is \\d+, and its real gN slot must be the LAST argument to
        hk. A bare \\d+ or a non-capturing (?:\\d+) gets no spoken-number-word
        support.
        """
        range_doc_ids = {
            "select-next-characters", "select-previous-characters",
            "select-next-words", "select-previous-words",
            "select-next-lines", "select-previous-lines",
            "select-next-paragraphs", "select-previous-paragraphs",
        }
        entries = {
            entry["doc_id"]: entry
            for entry in _production_pattern_entries()
            if entry.get("doc_id") in range_doc_ids
        }
        assert entries.keys() == range_doc_ids, (
            f"Missing range entries: {range_doc_ids - entries.keys()}"
        )
        for doc_id, entry in entries.items():
            pattern_text = entry["pattern"]
            assert r"(\d+)" in pattern_text, (
                f"{doc_id}: pattern must contain a real (\\d+) capture group body, "
                f"got {pattern_text!r}"
            )
            compiled = re.compile(pattern_text)
            assert compiled.groups == 1, (
                f"{doc_id}: expected exactly one real capture group, got "
                f"{compiled.groups} in {pattern_text!r}"
            )
            hk_actions = [a for a in entry["actions"] if a["function"] == "hk"]
            assert hk_actions, f"{doc_id}: no hk action"
            for action in hk_actions:
                assert action["params"][-1] == "g1", (
                    f"{doc_id}: hk params {action['params']!r} must end with g1, "
                    f"the real capture group's slot"
                )


# ============================================================================
# unselect that / clear selection
# ============================================================================

class TestUnselectAndClearSelection:
    """unselect that / clear selection -- a single arrow-key press (bead
    MECHANISM).

    KNOWN APPROXIMATION (bead DESCRIPTION, also recorded in the .toml
    fragment header): the arrow-key press that clears the selection also
    moves the caret to the selection's right edge, while Voice Access
    leaves the caret where it was. Not fixed here -- tracked for the help
    text and for the manual end-to-end pass (wh-voice-access-parity.1.10).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("spoken_phrase", "doc_id"),
        (
            ("unselect that", "unselect-that"),
            ("clear selection", "clear-selection"),
        ),
    )
    async def test_fires_single_arrow_key_press(
        self, parser, mock_app, spoken_phrase, doc_id
    ):
        matching_entries = [
            entry for entry in _production_pattern_entries()
            if entry.get("doc_id") == doc_id
        ]
        assert len(matching_entries) == 1, f"Missing pattern {doc_id}"

        assert await parser.parse_and_execute(spoken_phrase) is True
        mock_app.send_command.assert_called_once()
        payload = mock_app.send_command.call_args.args[0]
        assert payload == {
            "action": "press_key_action",
            "params": {"key": "right", "repeat": 1},
        }

    def test_neither_form_exists_before_this_bead(self):
        """Negative-claim evidence, captured once as a regression guard:
        before this bead, no production pattern matched these phrases at
        all (they fell through to dictation). grep -n "unselect|clear
        selection" services/wheelhouse/speech/config/patterns.toml over
        this worktree's file found no match on 2026-08-24.
        """
        entries = _production_pattern_entries()
        for phrase in ("unselect that", "clear selection"):
            matches = [
                entry.get("doc_id")
                for entry in entries
                if re.compile(entry["pattern"], re.IGNORECASE).search(phrase)
                and entry.get("doc_id") not in ("unselect-that", "clear-selection")
            ]
            assert not matches, (
                f"{phrase!r} unexpectedly matches an existing entry {matches}; "
                f"the bead assumed these phrases were unclaimed"
            )
