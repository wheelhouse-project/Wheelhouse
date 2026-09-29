"""Voice Access command aliases, bead wh-voice-access-parity.1.14.

David ordered these forms on 2026-09-25 (boss chat 11:39): "no space that",
"no space <words>", "move slider <direction> [n] times", "go|move
<direction> [n] times", "show numbers here", the four "show commands"
wordings, "snap window to <side>" without "the", the "move to" landmarks,
"move|select forward|backward", "equals sign", and "boldface that".

Each form is tied to its row the same way test_voice_access_alias_completeness
does it: the named row's regex must fullmatch the form, that row must come
FIRST among every anchored row that fullmatches it, and the live matcher must
fire on the form and consume all of it. The rows' actions are compared with
the actions the bead names, so a form that reaches the right row but presses
the wrong key still fails.

Acceptance 5 also names existing utterances that must keep their current
rows; TestExistingFormsKeepTheirRows is that list.
"""
import re
import sys
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.pattern_catalog import PatternCatalog
from speech.pattern_matcher import PatternMatcher
from tests.test_speech_pipeline import SpeechPipelineHarness
from tests.test_voice_access_punctuation_router import (
    _hotkey_key_lists,
    _inserted_text,
    _send_phrase,
)

_PATTERNS_PATH = Path(__file__).parent.parent / "speech" / "config" / "patterns.toml"


def _hk(*keys):
    return [{"function": "hk", "params": list(keys)}]


_COMPRESS = [
    {"function": "transform_selection", "params": ["compress"], "awaits_done": True}
]
_NO_SPACES = [{"function": "insert_raw_no_spaces", "params": ["g1"]}]
_PATTERN_MANAGER = [{"function": "open_pattern_manager"}]
_SHOW_NUMBERS = [{"function": "show_overlay_command", "awaits_done": True}]

# (spoken form, doc_id of the row that must own it, that row's actions,
#  whole_utterance_only expected on that row)
_NEW_FORMS = [
    # A1 / A2
    ("no space that", "no-space-that", _COMPRESS, True),
    ("no space hello world", "no-space", _NO_SPACES, False),
    # A3
    ("move slider up", "move-slider-up", _hk("up", "g1"), True),
    ("move slider up 3 times", "move-slider-up", _hk("up", "g1"), True),
    ("move slider down 2 times", "move-slider-down", _hk("down", "g1"), True),
    ("move slider left 1 time", "move-slider-left", _hk("left", "g1"), True),
    ("move slider right", "move-slider-right", _hk("right", "g1"), True),
    # A4
    ("go up", "nav-up-times", _hk("up", "g1"), True),
    ("go up 3 times", "nav-up-times", _hk("up", "g1"), True),
    ("move up 2 times", "nav-up-times", _hk("up", "g1"), True),
    ("go down 4 times", "nav-down-times", _hk("down", "g1"), True),
    ("move down", "nav-down-times", _hk("down", "g1"), True),
    ("go left 5 times", "nav-left-times", _hk("left", "g1"), True),
    ("move left", "nav-left-times", _hk("left", "g1"), True),
    ("go right 2 times", "nav-right-times", _hk("right", "g1"), True),
    ("move right 1 time", "nav-right-times", _hk("right", "g1"), True),
    # B1
    ("show numbers here", "show-numbers", _SHOW_NUMBERS, True),
    # B2
    ("what can i say", "show-commands", _PATTERN_MANAGER, True),
    ("show commands", "show-commands", _PATTERN_MANAGER, True),
    ("show all commands", "show-commands", _PATTERN_MANAGER, True),
    ("show command list", "show-commands", _PATTERN_MANAGER, True),
    # B3
    ("snap window to left", "snap-window-left", _hk("win", "left"), True),
    ("snap window to right", "snap-window-right", _hk("win", "right"), True),
    ("snap window to top", "snap-window-top", _hk("win", "alt", "up"), True),
    ("snap window to bottom", "snap-window-bottom", _hk("win", "alt", "down"), True),
    # B4
    ("move to top", "nav-go-top", _hk("ctrl", "home"), True),
    ("move to the bottom", "nav-go-bottom", _hk("ctrl", "end"), True),
    ("move to beginning of document", "nav-go-beginning-of-document", _hk("ctrl", "home"), True),
    ("move to start of the document", "nav-go-beginning-of-document", _hk("ctrl", "home"), True),
    ("move to end of document", "nav-go-end-of-document", _hk("ctrl", "end"), True),
    ("move to beginning of line", "nav-go-beginning-of-line", _hk("home"), True),
    ("move to end of line", "nav-go-end-of-line", _hk("end"), True),
    ("move to start of word", "nav-go-beginning-of-word", None, True),
    ("move to end of word", "nav-go-end-of-word", None, True),
    ("move to beginning of paragraph", "nav-go-beginning-of-paragraph", None, True),
    ("move to end of the paragraph", "nav-go-end-of-paragraph", None, True),
    # B5
    ("move forward 3 characters", "nav-right-characters", _hk("right", "g1"), True),
    ("move forward character", "nav-right-characters", _hk("right", "g1"), True),
    ("move backward 2 characters", "nav-left-characters", _hk("left", "g1"), True),
    ("select forward 3 characters", "select-next-characters", _hk("shift", "right", "g1"), True),
    ("select backward 3 characters", "select-previous-characters", _hk("shift", "left", "g1"), True),
    ("select forward 2 lines", "select-next-lines", _hk("shift", "down", "g1"), True),
    ("select backward 2 lines", "select-previous-lines", _hk("shift", "up", "g1"), True),
    # B7
    ("boldface that", "bold", _hk("ctrl", "b"), True),
]

# Acceptance 5: existing utterances and the rows they reach today (measured
# on b08002ee before any pattern changed).
_EXISTING_FORMS = [
    ("show chrome", "show-app", True),
    ("go to chrome", "switch-to-app", True),
    ("switch to chrome", "switch-to-app", True),
    ("go left 3 characters", "nav-left-characters", False),
    ("go right 3 characters", "nav-right-characters", False),
    ("go to top", "nav-go-top", False),
    ("move to beginning of selection", "nav-move-beginning-of-selection", False),
    ("move to end of selection", "nav-move-end-of-selection", False),
    ("snap window to the left", "snap-window-left", False),
    ("patterns", "pattern-manager", False),
    ("bold that", "bold", False),
    ("show numbers", "show-numbers", False),
    ("apply numbers", "show-numbers", False),
]


@pytest.fixture(scope="module")
def rows():
    """Every shipped pattern row, in file order, straight from the TOML."""
    return tomllib.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))["pattern"]


@pytest.fixture(scope="module")
def matcher():
    # user_patterns_file="" keeps the developer's personal
    # data/user_patterns.toml out of the catalog.
    return PatternMatcher(
        PatternCatalog(str(_PATTERNS_PATH), user_patterns_file="")
    )


def _first_owner(rows, form):
    """(index, row) of the first anchored shipped row that fullmatches `form`."""
    for index, row in enumerate(rows):
        source = row["pattern"]
        if not source.startswith("^"):
            continue
        if re.compile(source, re.IGNORECASE).fullmatch(form):
            return index, row
    return None, None


def _by_doc_id(rows, doc_id):
    for row in rows:
        if row.get("doc_id") == doc_id:
            return row
    return None


class TestEveryNewFormReachesItsRow:
    @pytest.mark.parametrize("form,doc_id,actions,whole", _NEW_FORMS)
    def test_the_named_row_owns_the_form(self, rows, form, doc_id, actions, whole):
        index, row = _first_owner(rows, form)
        assert row is not None, (
            f"no shipped pattern row fullmatches {form!r}; bead "
            f"wh-voice-access-parity.1.14 names it for doc_id {doc_id!r}"
        )
        assert row.get("doc_id") == doc_id, (
            f"{form!r} is claimed first by doc_id {row.get('doc_id')!r} "
            f"(row {index}, {row['pattern']!r}), not by {doc_id!r}"
        )

    @pytest.mark.parametrize("form,doc_id,actions,whole", _NEW_FORMS)
    def test_the_row_runs_the_named_action(self, rows, form, doc_id, actions, whole):
        if actions is None:
            pytest.skip("the row's actions predate this bead and are unchanged")
        row = _by_doc_id(rows, doc_id)
        assert row is not None, f"doc_id {doc_id!r} is missing"
        assert row["actions"] == actions, (
            f"doc_id {doc_id!r} runs {row['actions']!r}; {form!r} must run "
            f"{actions!r}"
        )

    @pytest.mark.parametrize("form,doc_id,actions,whole", _NEW_FORMS)
    def test_whole_utterance_flag(self, rows, form, doc_id, actions, whole):
        row = _by_doc_id(rows, doc_id)
        assert row is not None, f"doc_id {doc_id!r} is missing"
        assert bool(row.get("whole_utterance_only", False)) is whole, (
            f"doc_id {doc_id!r} has whole_utterance_only="
            f"{row.get('whole_utterance_only', False)!r}; acceptance 1 "
            f"expects {whole!r}"
        )

    @pytest.mark.parametrize("form,doc_id,actions,whole", _NEW_FORMS)
    def test_the_live_matcher_fires_and_consumes_the_whole_form(
        self, matcher, form, doc_id, actions, whole
    ):
        result = matcher.match_complete(form, hotword_active=False)
        assert result is not None and result.matched, (
            f"the shipped matcher does not fire on {form!r} without the hotword"
        )
        assert result.matched_text == form, (
            f"{form!r} matched only {result.matched_text!r}"
        )
        assert result.remainder == "" and result.before_remainder == ""


class TestExistingFormsKeepTheirRows:
    @pytest.mark.parametrize("form,doc_id,needs_hotword", _EXISTING_FORMS)
    def test_first_owner_is_unchanged(self, rows, form, doc_id, needs_hotword):
        index, row = _first_owner(rows, form)
        assert row is not None and row.get("doc_id") == doc_id, (
            f"{form!r} now reaches "
            f"{row.get('doc_id') if row else None!r}, not {doc_id!r}"
        )

    @pytest.mark.parametrize(
        "form,doc_id",
        [(f, d) for f, d, _a, _w in _NEW_FORMS]
        + [(f, d) for f, d, _h in _EXISTING_FORMS],
    )
    def test_the_live_first_word_index_reaches_the_row(
        self, matcher, rows, form, doc_id
    ):
        """The router looks a form up by its first word. A row missing
        from that word's list is never tried, so "go left 3 characters"
        fell to cursor-navigate and waited the greedy timeout. Index
        entries carry no doc_id, so the owner is named by its actions."""
        word = form.split()[0].lower()
        owner = next(
            (
                data
                for compiled, _type, data in matcher.catalog.first_words.get(word, [])
                if compiled.fullmatch(form)
            ),
            None,
        )
        expected = _by_doc_id(rows, doc_id)["actions"]
        assert owner is not None and owner["actions"] == expected, (
            f"the first row indexed under {word!r} for {form!r} runs "
            f"{owner['actions'] if owner else None!r}, not {doc_id!r}"
        )

    @pytest.mark.parametrize(
        "form,doc_id",
        [(f, d) for f, d, _a, _w in _NEW_FORMS]
        + [(f, d) for f, d, _h in _EXISTING_FORMS],
    )
    def test_the_pattern_manager_display_names_the_form(
        self, rows, form, doc_id
    ):
        """The Pattern Manager shows a row's trigger through
        _trigger_display, which drops a nested alternation group:
        "show (?:all )?commands" displayed as "commands|show command
        list". A row without a capture group must name every form. The
        display leaves out an optional word such as "(?:the )?" by
        design, so the form is compared without those words."""
        from speech.pattern_manager import PatternManager

        pattern = _by_doc_id(rows, doc_id)["pattern"]
        if re.search(r"\((?!\?)", pattern):
            pytest.skip("a row with a capture group displays a placeholder")
        optional = set(re.findall(r"\(\?:([a-z]+) \)\?", pattern))
        # wh-voice-access-parity.1.15 made "that" optional with a leading
        # space: "(?: that)?" and, on the bold row, "(?: text| that)?".
        optional |= set(re.findall(r"\(\?: ([a-z]+)\)\?", pattern))
        for group in re.findall(r"\(\?:((?: [a-z]+\|)+ [a-z]+)\)\?", pattern):
            optional |= {word.strip() for word in group.split("|")}
        spoken = " ".join(w for w in form.split() if w not in optional)
        display = PatternManager._trigger_display(pattern)
        assert spoken in display, f"{pattern!r} displays as {display!r}"

    def test_no_space_that_sits_before_no_space(self, rows):
        positions = {row.get("doc_id"): i for i, row in enumerate(rows)}
        assert positions["no-space-that"] < positions["no-space"]


class TestNoSpaceAction:
    def test_removes_every_space_like_compress(self):
        from speech.actions import ActionFunctions

        actions = ActionFunctions(MagicMock())
        payload = actions.get_functions()["insert_raw_no_spaces"]("hello big world")
        assert payload == {
            "action": "raw_insert_text",
            "params": {"text": "hellobigworld"},
        }

    def test_catalog_lists_the_action(self):
        from speech.action_catalog import ACTION_CATALOG

        names = [entry["name"] for entry in ACTION_CATALOG]
        assert "insert_raw_no_spaces" in names


@pytest.fixture
async def harness():
    h = SpeechPipelineHarness()
    await h.start()
    yield h
    await h.stop()


class TestSpokenThroughThePipeline:
    @pytest.mark.asyncio
    async def test_no_space_types_the_words_joined(self, harness):
        await _send_phrase(harness, ("no", "space", "hello", "world"))

        raw = [
            output.params.get("text")
            for output in harness.get_outputs()
            if output.action == "raw_insert_text"
        ]
        assert raw == ["helloworld"], f"raw inserts: {raw!r}"
        assert _inserted_text(harness) == []

    @pytest.mark.asyncio
    async def test_go_up_3_times_presses_up(self, harness):
        await _send_phrase(harness, ("go", "up", "3", "times"))

        assert ["up"] in _hotkey_key_lists(harness)
        assert _inserted_text(harness) == []

    @pytest.mark.asyncio
    async def test_equals_sign_inside_a_sentence_types_the_character(self, harness):
        await _send_phrase(harness, ("x", "equals", "sign", "y"))

        inserted = " ".join(_inserted_text(harness))
        assert "=" in inserted, f"inserted={inserted!r}"
        assert "equals sign" not in inserted

    @pytest.mark.asyncio
    async def test_equal_sign_inside_a_sentence_still_types_the_character(
        self, harness
    ):
        await _send_phrase(harness, ("x", "equal", "sign", "y"))

        inserted = " ".join(_inserted_text(harness))
        assert "=" in inserted, f"inserted={inserted!r}"
        assert "equal sign" not in inserted
