"""Voice Access alias follow-up, bead wh-voice-access-parity.1.15.

David's check of 1.14 (answered 19:06 2026-09-25) asked for these forms:
the word "that" optional in every pattern that required it (F1), "bold
face" with a space wherever "boldface" works (F2), "select forward|backward
[n] words" (F3), and "equals sine" typing "=" (F5, his option 1 at 19:12).
F4 (the Pattern Manager comes to the front) is tested in
test_pattern_manager_dialog.py next to the other _open_pattern_manager test.

The layout copies test_voice_access_aliases_1_14.py: each form is tied to
its row by first-owner order in the TOML, by the row's actions and
whole_utterance_only flag, by the live matcher, and by the live first-word
index; then the pipeline harness proves the spoken result.
"""
import asyncio
import re
import sys
import tomllib
from pathlib import Path

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


def _transform(name):
    return [
        {"function": "transform_selection", "params": [name], "awaits_done": True}
    ]


_BOLD = _hk("ctrl", "b")
_ITALICS = _hk("ctrl", "i")
_UNSELECT = [{"function": "press", "params": ["right"]}]
_SELECT_WORDS_RIGHT = _hk("shift", "ctrl", "right", "g1")
_SELECT_WORDS_LEFT = _hk("shift", "ctrl", "left", "g1")

# (spoken form, doc_id of the row that must own it, that row's actions,
#  whole_utterance_only expected on that row)
_FORMS = [
    # F1: "that" is optional; the "that" forms keep working.
    ("unselect", "unselect-that", _UNSELECT, True),
    ("unselect that", "unselect-that", _UNSELECT, True),
    ("italicize", "italics", _ITALICS, True),
    ("italicize that", "italics", _ITALICS, True),
    ("italics", "italics", _ITALICS, True),
    ("all caps", "uppercase-all-caps-that", _transform("uppercase"), True),
    ("all caps that", "uppercase-all-caps-that", _transform("uppercase"), True),
    ("no caps", "lowercase-no-caps-that", _transform("lowercase"), True),
    ("no caps that", "lowercase-no-caps-that", _transform("lowercase"), True),
    ("cap", "capitalize-that", _transform("capitalize"), True),
    ("cap that", "capitalize-that", _transform("capitalize"), True),
    ("no space", "no-space-that", _transform("compress"), True),
    ("no space that", "no-space-that", _transform("compress"), True),
    ("bold", "bold", _BOLD, True),
    ("bold that", "bold", _BOLD, True),
    ("bold text", "bold", _BOLD, True),
    ("boldface", "bold", _BOLD, True),
    ("boldface that", "bold", _BOLD, True),
    ("boldface text", "bold", _BOLD, True),
    # F2: "bold face" with a space.
    ("bold face", "bold", _BOLD, True),
    ("bold face that", "bold", _BOLD, True),
    ("bold face text", "bold", _BOLD, True),
    # F3: forward / backward words, count optional.
    ("select forward words", "select-next-words", _SELECT_WORDS_RIGHT, True),
    ("select forward word", "select-next-words", _SELECT_WORDS_RIGHT, True),
    ("select forward 2 words", "select-next-words", _SELECT_WORDS_RIGHT, True),
    ("select backward words", "select-previous-words", _SELECT_WORDS_LEFT, True),
    ("select backward 3 words", "select-previous-words", _SELECT_WORDS_LEFT, True),
    ("select next 2 words", "select-next-words", _SELECT_WORDS_RIGHT, True),
    ("select previous 2 words", "select-previous-words", _SELECT_WORDS_LEFT, True),
    ("select last word", "select-previous-words", _SELECT_WORDS_LEFT, True),
]

# Longer utterances that must keep the row they reach today.
_EXISTING_FORMS = [
    ("no space hello world", "no-space"),
    ("bold next 3 words", "bold-next-words"),
    ("bold previous 2 words", "bold-prev-words"),
    ("italicize previous 2 words", "italicize-prev-words"),
    ("italicize next 3 words", "italicize-next-words"),
    ("capitalize", "capitalize"),
    ("capitalize that", "capitalize"),
    ("select forward 2 lines", "select-next-lines"),
    ("select backward 2 characters", "select-previous-characters"),
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


class TestEveryFormReachesItsRow:
    @pytest.mark.parametrize("form,doc_id,actions,whole", _FORMS)
    def test_the_named_row_owns_the_form(self, rows, form, doc_id, actions, whole):
        index, row = _first_owner(rows, form)
        assert row is not None, (
            f"no shipped pattern row fullmatches {form!r}; bead "
            f"wh-voice-access-parity.1.15 names it for doc_id {doc_id!r}"
        )
        assert row.get("doc_id") == doc_id, (
            f"{form!r} is claimed first by doc_id {row.get('doc_id')!r} "
            f"(row {index}, {row['pattern']!r}), not by {doc_id!r}"
        )

    @pytest.mark.parametrize("form,doc_id,actions,whole", _FORMS)
    def test_the_row_runs_the_named_action(self, rows, form, doc_id, actions, whole):
        row = _by_doc_id(rows, doc_id)
        assert row is not None, f"doc_id {doc_id!r} is missing"
        assert row["actions"] == actions, (
            f"doc_id {doc_id!r} runs {row['actions']!r}; {form!r} must run "
            f"{actions!r}"
        )

    @pytest.mark.parametrize("form,doc_id,actions,whole", _FORMS)
    def test_whole_utterance_flag(self, rows, form, doc_id, actions, whole):
        row = _by_doc_id(rows, doc_id)
        assert row is not None, f"doc_id {doc_id!r} is missing"
        assert bool(row.get("whole_utterance_only", False)) is whole, (
            f"doc_id {doc_id!r} has whole_utterance_only="
            f"{row.get('whole_utterance_only', False)!r}; the bead "
            f"expects {whole!r}"
        )

    @pytest.mark.parametrize("form,doc_id,actions,whole", _FORMS)
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
    @pytest.mark.parametrize("form,doc_id", _EXISTING_FORMS)
    def test_first_owner_is_unchanged(self, rows, form, doc_id):
        index, row = _first_owner(rows, form)
        assert row is not None and row.get("doc_id") == doc_id, (
            f"{form!r} now reaches "
            f"{row.get('doc_id') if row else None!r}, not {doc_id!r}"
        )

    @pytest.mark.parametrize(
        "form,doc_id",
        [(f, d) for f, d, _a, _w in _FORMS] + list(_EXISTING_FORMS),
    )
    def test_the_live_first_word_index_reaches_the_row(
        self, matcher, rows, form, doc_id
    ):
        """The router looks a form up by its first word, and a row missing
        from that word's list is never tried. The index does not expand a
        "?" quantifier inside a group, which is why "bold face" is an
        explicit alternative. Index entries carry no doc_id, so the owner
        is named by its actions."""
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

    @pytest.mark.parametrize("form,doc_id", [(f, d) for f, d, _a, _w in _FORMS])
    def test_the_pattern_manager_display_names_the_form(
        self, rows, form, doc_id
    ):
        """The Pattern Manager shows a row's trigger through
        _trigger_display, which leaves out an optional word such as
        "(?: that)?" by design, so the form is compared without those
        words. A row with a capture group displays a placeholder."""
        from speech.pattern_manager import PatternManager

        pattern = _by_doc_id(rows, doc_id)["pattern"]
        if re.search(r"\((?!\?)", pattern):
            pytest.skip("a row with a capture group displays a placeholder")
        optional = set(re.findall(r"\(\?: ([a-z]+)\)\?", pattern))
        # The bold row's flat optional tail "(?: text| that)?".
        for group in re.findall(r"\(\?:((?: [a-z]+\|)+ [a-z]+)\)\?", pattern):
            optional |= {word.strip() for word in group.split("|")}
        spoken = " ".join(w for w in form.split() if w not in optional)
        display = PatternManager._trigger_display(pattern)
        assert spoken in display, f"{pattern!r} displays as {display!r}"

    def test_no_space_that_sits_before_no_space(self, rows):
        positions = {row.get("doc_id"): i for i, row in enumerate(rows)}
        assert positions["no-space-that"] < positions["no-space"]


class TestEqualsSine:
    """F5, David's option 1: "equals sine" types "=" like "equals sign"."""

    @pytest.mark.parametrize(
        "spoken", ["equals sign", "equal sign", "equals sine", "equal sine"]
    )
    def test_the_equal_sign_row_matches(self, rows, spoken):
        row = _by_doc_id(rows, "punct-equal-sign")
        assert re.search(row["pattern"], f"x {spoken} y", re.IGNORECASE), (
            f"{row['pattern']!r} does not find {spoken!r}"
        )

    @pytest.mark.parametrize("spoken", ["sine", "the sine of x", "cosine"])
    def test_sine_alone_is_not_an_equal_sign(self, rows, spoken):
        row = _by_doc_id(rows, "punct-equal-sign")
        assert not re.search(row["pattern"], spoken, re.IGNORECASE)


@pytest.fixture
async def harness():
    h = SpeechPipelineHarness()
    await h.start()
    yield h
    await h.stop()


def _non_dictation_actions(harness):
    return [
        action
        for action in harness.mock_app.get_all_actions()
        if action not in ("intelligent_insert_text", "end_utterance")
    ]


class TestSpokenThroughThePipeline:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words",
        [
            ("unselect",),
            ("italicize",),
            ("all", "caps"),
            ("no", "caps"),
            ("cap",),
            ("no", "space"),
            ("bold",),
            ("boldface",),
            ("bold", "face"),
            ("bold", "face", "that"),
            ("cap", "that"),
            ("no", "space", "that"),
        ],
    )
    async def test_the_whole_utterance_runs_a_command(self, harness, words):
        await _send_phrase(harness, words)

        assert _inserted_text(harness) == [], (
            f"{' '.join(words)!r} was typed: {_inserted_text(harness)!r}"
        )
        assert _non_dictation_actions(harness), (
            f"{' '.join(words)!r} ran no command"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words", [("bold",), ("bold", "face"), ("bold", "face", "that")]
    )
    async def test_bold_forms_press_ctrl_b(self, harness, words):
        await _send_phrase(harness, words)

        assert ["ctrl", "b"] in _hotkey_key_lists(harness)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words",
        [
            ("I", "need", "a", "cap", "for", "it"),
            ("please", "unselect", "the", "row"),
            ("we", "italicize", "book", "titles"),
            ("write", "it", "in", "all", "caps", "today"),
            ("there", "are", "no", "caps", "here"),
            ("there", "is", "no", "space", "left"),
            ("make", "it", "bold", "please"),
            ("a", "bold", "face", "font"),
            ("a", "boldface", "font"),
            # The bare word LEADING the sentence. whole_utterance_only is
            # what keeps these dictated: without it the router's prefix
            # search runs the command and dictates only the rest. ("no
            # space left" is not here: the no-space row owns it by design.)
            ("unselect", "the", "row"),
            ("italicize", "book", "titles"),
            ("all", "caps", "is", "loud"),
            ("no", "caps", "here"),
            ("cap", "the", "bottle"),
            ("bold", "ideas", "win"),
        ],
    )
    async def test_the_bare_word_inside_a_sentence_is_dictation(
        self, harness, words
    ):
        await _send_phrase(harness, words)

        offending = _non_dictation_actions(harness)
        assert not offending, (
            f"{' '.join(words)!r} ran {offending!r}; the whole sentence must "
            "be ordinary dictation"
        )
        dictated = " ".join(_inserted_text(harness))
        for word in words:
            assert word in dictated, (
                f"{word!r} was lost from dictation; dictated={dictated!r}"
            )

    @pytest.mark.asyncio
    async def test_no_space_with_words_still_types_them_joined(self, harness):
        await _send_phrase(harness, ("no", "space", "hello", "world"))

        raw = [
            output.params.get("text")
            for output in harness.get_outputs()
            if output.action == "raw_insert_text"
        ]
        assert raw == ["helloworld"], f"raw inserts: {raw!r}"
        assert _inserted_text(harness) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words,keys,repeat",
        [
            (("bold", "next", "three", "words"), ["shift", "ctrl", "right"], 3),
            (("italicize", "previous", "two", "words"), ["shift", "ctrl", "left"], 2),
        ],
    )
    async def test_range_forms_still_reach_their_range_rows(
        self, harness, words, keys, repeat
    ):
        await _send_phrase(harness, words)

        hotkeys = [
            output.params
            for output in harness.get_outputs()
            if output.action == "hotkey_action"
        ]
        assert {"keys": keys, "repeat": repeat} in hotkeys, (
            f"{' '.join(words)!r} sent {hotkeys!r}"
        )
        assert _inserted_text(harness) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words,keys,repeat",
        [
            (("select", "forward", "words"), ["shift", "ctrl", "right"], 1),
            (("select", "forward", "2", "words"), ["shift", "ctrl", "right"], 2),
            (("select", "backward", "three", "words"), ["shift", "ctrl", "left"], 3),
            (("select", "backward", "word"), ["shift", "ctrl", "left"], 1),
        ],
    )
    async def test_select_forward_backward_words(self, harness, words, keys, repeat):
        await _send_phrase(harness, words)

        hotkeys = [
            output.params
            for output in harness.get_outputs()
            if output.action == "hotkey_action"
        ]
        assert hotkeys == [{"keys": keys, "repeat": repeat}], (
            f"{' '.join(words)!r} sent {hotkeys!r}; "
            f"typed {_inserted_text(harness)!r}"
        )
        assert _inserted_text(harness) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words",
        [("x", "equals", "sine", "y"), ("x", "equal", "sine", "y")],
    )
    async def test_equals_sine_inside_a_sentence_types_the_character(
        self, harness, words
    ):
        await _send_phrase(harness, words)

        inserted = " ".join(_inserted_text(harness))
        assert "=" in inserted, f"inserted={inserted!r}"
        assert "sine" not in inserted, f"inserted={inserted!r}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("second", ["sign", "sine"])
    async def test_equal_sine_alone_types_the_character(self, harness, second):
        await _send_phrase(harness, ("equal", second))

        inserted = " ".join(_inserted_text(harness))
        assert "=" in inserted, f"inserted={inserted!r}"
        assert second not in inserted, f"inserted={inserted!r}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("second", ["sign", "sine"])
    async def test_a_pause_after_equals_still_types_the_character(
        self, harness, second
    ):
        """A 300 ms gap before the second word, inside one utterance.
        The gap is shorter than the router's 400 ms replacement timeout
        (router.py replacement_timeout_ms), the budget a spoken pause
        between the two words has to fit in; "sign" and "sine" must behave
        the same."""
        words = ("x", "equals", second, "y")
        for index, word in enumerate(words):
            await harness.send_word(
                word,
                start_of_utterance=index == 0,
                end_of_utterance=index == len(words) - 1,
                delay_before_ms=300 if word == second else (50 if index else 0),
            )
        await harness.send_utterance_end_marker(harness._utterance_counter)
        await asyncio.sleep(0.1)

        inserted = " ".join(_inserted_text(harness))
        assert "=" in inserted, f"inserted={inserted!r}"
        assert second not in inserted, f"inserted={inserted!r}"
