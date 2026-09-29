"""The word "go" works wherever a command pattern accepts "move" (wh-go-synonym-for-move).

David, 2026-09-26: "Make the word "go" a synonym for the word "move" in
command patterns". Nine shipped patterns accepted "move" but not "go":
nav-left-characters ("move backward"), nav-right-characters ("move
forward"), the four move-slider entries, the two "move to the beginning/end
of the selection" entries, and grid-move-here ("move here"). Before this
bead each "go" form reached the '^go (.+)' catch-all (cursor-navigate),
or switch-to-app under the wake word for the "go to ... selection" forms,
and the cursor parser has no forward, backward, slider, selection, or here
words, so the phrase was typed as text.

The tests here prove three things: each "go" form reaches the same entry as
its "move" twin and sends the same thing; the phrases that already started
with "go" keep their entries; and "go here" with the grid closed still types
"go here" as text (the boss ruling on the bead).
"""

import re
import sys
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

project_root = Path(__file__).resolve().parents[3]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
service_dir = project_root / "services" / "wheelhouse"
if str(service_dir) not in sys.path:
    sys.path.insert(1, str(service_dir))

from services.wheelhouse.speech.pattern_catalog import PatternCatalog
from services.wheelhouse.speech.command_engine import TextParser
from services.wheelhouse.speech.pattern_transform import transform_pattern

from tests.test_grid_closed_fallback_retraction import (
    _ClosedGridLc,
    _make_processor,
)
from tests.test_speech_pipeline import SpeechPipelineHarness
from tests.test_voice_access_punctuation_router import (
    _hotkey_key_lists,
    _inserted_text,
    _send_phrase,
)

PATTERNS_FILE = service_dir / "speech" / "config" / "patterns.toml"


def _production_pattern_entries():
    with PATTERNS_FILE.open("rb") as handle:
        return tomllib.load(handle)["pattern"]


def _first_match_doc_id(spoken_phrase):
    """Walk the built-in entries in file order, the way the catalog does.

    Same walk as test_direction_left_right_words: the catalog widens every
    (\\d+) to (\\w+) at load time, so the same transform is applied. The
    walk ignores requires_hotword, so it also answers for the wake-word
    case, where switch-to-app is eligible.
    """
    for entry in _production_pattern_entries():
        compiled = re.compile(transform_pattern(entry["pattern"])[0], re.IGNORECASE)
        if compiled.search(spoken_phrase):
            return entry.get("doc_id")
    return None


async def _calls_for(spoken_phrase):
    """Execute one phrase on a fresh parser; return what it sent."""
    app = Mock()
    app.send_command = AsyncMock()
    app.send_request = AsyncMock(return_value={"status": "success"})
    handler = Mock()
    handler.app = app
    parser = TextParser(handler, PatternCatalog(str(PATTERNS_FILE)))
    executed = await parser.parse_and_execute(spoken_phrase, authorized_command=True)
    return executed, app.send_command.call_args_list, app.send_request.call_args_list


# (go form, move twin, doc_id both must reach). Keyboard entries only; the
# grid entry "go here" is tested separately below, because its action
# needs a grid.
GO_KEY_FORMS = [
    ("go backward 3 characters", "move backward 3 characters", "nav-left-characters"),
    ("go backward character", "move backward character", "nav-left-characters"),
    ("go backward two characters", "move backward two characters", "nav-left-characters"),
    ("go forward 3 characters", "move forward 3 characters", "nav-right-characters"),
    ("go forward character", "move forward character", "nav-right-characters"),
    ("go slider up", "move slider up", "move-slider-up"),
    ("go slider up 3 times", "move slider up 3 times", "move-slider-up"),
    ("go slider down", "move slider down", "move-slider-down"),
    ("go slider down two times", "move slider down two times", "move-slider-down"),
    ("go slider left 2 times", "move slider left 2 times", "move-slider-left"),
    ("go slider right", "move slider right", "move-slider-right"),
    ("go slider write 4 times", "move slider write 4 times", "move-slider-right"),
    ("go to the beginning of the selection", "move to the beginning of the selection",
     "nav-move-beginning-of-selection"),
    ("go to beginning of selection", "move to beginning of selection",
     "nav-move-beginning-of-selection"),
    ("go to the end of the selection", "move to the end of the selection",
     "nav-move-end-of-selection"),
    ("go to end of selection", "move to end of selection", "nav-move-end-of-selection"),
]

GO_GRID_FORMS = [
    ("go here", "move here", "grid-move-here"),
    ("go here.", "move here.", "grid-move-here"),
]


class TestGoReachesTheMoveEntry:

    @pytest.mark.parametrize(("go_form", "move_form", "doc_id"), GO_KEY_FORMS + GO_GRID_FORMS)
    def test_first_match_is_the_move_twin_entry(self, go_form, move_form, doc_id):
        assert _first_match_doc_id(move_form) == doc_id
        assert _first_match_doc_id(go_form) == doc_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("go_form", "move_form", "doc_id"), GO_KEY_FORMS)
    async def test_go_form_sends_what_the_move_form_sends(self, go_form, move_form, doc_id):
        move_executed, move_commands, move_requests = await _calls_for(move_form)
        assert move_executed is True
        assert move_commands or move_requests, f"{move_form!r} sent nothing"
        assert await _calls_for(go_form) == (True, move_commands, move_requests)


# Phrases that already started with "go" or "move", and the entry each
# reached before this bead (measured against dev 6dbce317). None may move.
UNCHANGED_FORMS = [
    ("go home", "cursor-navigate"),
    ("go end", "cursor-navigate"),
    ("go to end", "nav-go-end"),
    ("go right two words", "nav-right-words"),
    ("go home then grab to end", "cursor-navigate"),
    ("go left 2 words then grab right", "cursor-navigate"),
    ("go to notepad", "switch-to-app"),
    ("go up", "nav-up-times"),
    ("go to the top", "nav-go-top"),
    # "go here" followed by more words is not the grid command.
    ("go here and there", "cursor-navigate"),
    ("go there", "cursor-navigate"),
    ("go forward", "cursor-navigate"),
    ("go backward", "cursor-navigate"),
    ("move backward 3 characters", "nav-left-characters"),
    ("move left 2 characters", "nav-left-characters"),
    ("go left 2 characters", "nav-left-characters"),
    ("move forward 3 characters", "nav-right-characters"),
    ("go right 3 characters", "nav-right-characters"),
    ("go write 3 characters", "nav-right-characters"),
    ("move slider up 3 times", "move-slider-up"),
    ("move slider down", "move-slider-down"),
    ("move slider left", "move-slider-left"),
    ("move slider right 2 times", "move-slider-right"),
    ("move slider write", "move-slider-right"),
    ("move to the beginning of the selection", "nav-move-beginning-of-selection"),
    ("move to end of selection", "nav-move-end-of-selection"),
    ("move here", "grid-move-here"),
    ("move here.", "grid-move-here"),
]


class TestExistingPhrasesKeepTheirEntry:

    @pytest.mark.parametrize(("spoken_phrase", "doc_id"), UNCHANGED_FORMS)
    def test_existing_phrase_reaches_the_same_entry(self, spoken_phrase, doc_id):
        assert _first_match_doc_id(spoken_phrase) == doc_id


@pytest.fixture
async def nav_harness():
    harness = SpeechPipelineHarness()
    await harness.start()
    yield harness
    await harness.stop()


class TestRouterWithTheWakeWord:
    """Word-by-word through the production router, as test_va_navigation_router.

    switch-to-app ('^(?:switch to|go to)\\s+(.+)$', requires_hotword) sits
    below the selection entries, so under the wake word "go to the
    beginning of the selection" must navigate and not activate an app.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("words", "expected_keys"),
        (
            (("go", "to", "the", "beginning", "of", "the", "selection"), ["left"]),
            (("go", "to", "the", "end", "of", "the", "selection"), ["right"]),
        ),
    )
    async def test_go_to_selection_navigates_under_hotword(
        self, nav_harness, words, expected_keys
    ):
        await _send_phrase(nav_harness, words, prefix=("x-ray",))

        assert "activate_window" not in nav_harness.mock_app.get_all_actions(), (
            f"{' '.join(words)!r} activated an app instead of navigating"
        )
        assert expected_keys in _hotkey_key_lists(nav_harness)
        assert _inserted_text(nav_harness) == []

    @pytest.mark.asyncio
    async def test_go_to_app_name_still_activates_under_hotword(self, nav_harness):
        await _send_phrase(nav_harness, ("go", "to", "notepad"), prefix=("x-ray",))

        assert "activate_window" in nav_harness.mock_app.get_all_actions()

    @pytest.mark.asyncio
    async def test_go_home_then_grab_to_end_chain_still_parses(self, nav_harness):
        await _send_phrase(
            nav_harness, ("go", "home", "then", "grab", "to", "end")
        )

        hotkeys = _hotkey_key_lists(nav_harness)
        assert ["home"] in hotkeys
        assert ["shift", "end"] in hotkeys
        assert _inserted_text(nav_harness) == []


class _OpenGridLc(_ClosedGridLc):
    """handle_grid_command reporting the grid OPEN (consumes the command)."""

    async def handle_grid_command(self, command, trace_id, **kwargs):
        self.calls.append((command, kwargs))
        return True


class TestGoHere:
    """grid-move-here: "go here" is the grid's move_here with the grid open,
    and types "go here" as text with the grid closed (boss ruling, 2026-09-26).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("utterance", ["go here", "move here"])
    async def test_grid_open_moves_the_pointer(self, utterance):
        proc, app = _make_processor()
        grid = _OpenGridLc()
        proc.text_parser.speech_handler.logic_controller = grid

        await proc._execute_command(utterance)

        assert [command for command, _kwargs in grid.calls] == ["move_here"]
        assert app.inserts() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("utterance", ["go here", "go here.", "move here"])
    async def test_grid_closed_types_the_words(self, utterance):
        proc, app = _make_processor()

        await proc._execute_command(utterance)

        assert app.inserts() == [utterance]
        assert app.commands == []
        assert proc.text_parser.last_executed_pattern_type == "dictation_fallback"
