"""Direction words right, write, and left (wh-direction-left-right-words).

David, 2026-09-26: every forward and backward direction pattern must also
accept "right" or "write" for forward and "left" for backward. Parakeet can
hear "right" as "write", so both spellings must work.

Scope: the select next/previous characters, words, and lines entries, and
nav-right-characters ("go write" and "move write"). The paragraph select
entries have no forward/backward form and stay unchanged.
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

PATTERNS_FILE = service_dir / "speech" / "config" / "patterns.toml"


def _production_pattern_entries():
    with PATTERNS_FILE.open("rb") as handle:
        return tomllib.load(handle)["pattern"]


@pytest.fixture
def mock_app():
    app = Mock()
    app.send_command = AsyncMock()
    app.send_request = AsyncMock(return_value={"status": "success"})
    return app


@pytest.fixture
def parser(mock_app):
    """TextParser with authorized_command=True, as in test_va_select_range."""
    mock_handler = Mock()
    mock_handler.app = mock_app
    p = TextParser(mock_handler, PatternCatalog(str(PATTERNS_FILE)))
    orig = p.parse_and_execute
    p.parse_and_execute = (
        lambda text, **kw: orig(text, **{"authorized_command": True, **kw})
    )
    return p


# (spoken phrase, doc_id, hk keys, repeat count)
NEW_FORMS = [
    # select forward: right and write
    ("select right 3 words", "select-next-words", ["shift", "ctrl", "right"], 3),
    ("select write 3 words", "select-next-words", ["shift", "ctrl", "right"], 3),
    ("select right word", "select-next-words", ["shift", "ctrl", "right"], 1),
    ("select write two words", "select-next-words", ["shift", "ctrl", "right"], 2),
    ("select right 2 characters", "select-next-characters", ["shift", "right"], 2),
    ("select write 2 characters", "select-next-characters", ["shift", "right"], 2),
    ("select write character", "select-next-characters", ["shift", "right"], 1),
    ("select right 4 lines", "select-next-lines", ["shift", "down"], 4),
    ("select write four lines", "select-next-lines", ["shift", "down"], 4),
    # select backward: left
    ("select left 4 lines", "select-previous-lines", ["shift", "up"], 4),
    ("select left line", "select-previous-lines", ["shift", "up"], 1),
    ("select left 3 words", "select-previous-words", ["shift", "ctrl", "left"], 3),
    ("select left five words", "select-previous-words", ["shift", "ctrl", "left"], 5),
    ("select left 2 characters", "select-previous-characters", ["shift", "left"], 2),
    ("select left character", "select-previous-characters", ["shift", "left"], 1),
    # move/go forward by character: write
    ("move write 5 characters", "nav-right-characters", ["right"], 5),
    ("move write character", "nav-right-characters", ["right"], 1),
    ("go write 3 characters", "nav-right-characters", ["right"], 3),
    ("go write two characters", "nav-right-characters", ["right"], 2),
]

# Forms that existed before this bead, with the entries they reach. The
# changed alternations must not move any of them.
EXISTING_FORMS = [
    ("select next 3 words", "select-next-words"),
    ("select forward 3 characters", "select-next-characters"),
    ("select backward 2 lines", "select-previous-lines"),
    ("select last word", "select-previous-words"),
    ("go right 3 characters", "nav-right-characters"),
    ("move forward 3 characters", "nav-right-characters"),
    ("move left 2 characters", "nav-left-characters"),
    ("move backward 2 characters", "nav-left-characters"),
    ("go right 3 words", "nav-right-words"),
    ("move left 2 words", "nav-left-words"),
    ("select next 2 paragraphs", "select-next-paragraphs"),
    ("select last paragraph", "select-previous-paragraphs"),
    ("select right now", "select-phrase"),
    ("select left over text", "select-phrase"),
    ("select write something", "select-phrase"),
]


def _first_match_doc_id(spoken_phrase):
    """Walk the built-in entries in file order, the way the catalog does.

    The catalog widens every (\\d+) to (\\w+) at load time, so apply the
    same transform; spoken numbers such as "two" then match.
    """
    for entry in _production_pattern_entries():
        compiled = re.compile(transform_pattern(entry["pattern"])[0], re.IGNORECASE)
        if compiled.search(spoken_phrase):
            return entry.get("doc_id")
    return None


class TestNewDirectionWords:

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("spoken_phrase", "doc_id", "keys", "repeat"), NEW_FORMS)
    async def test_new_form_fires_same_keys_and_count(
        self, parser, mock_app, spoken_phrase, doc_id, keys, repeat
    ):
        assert await parser.parse_and_execute(spoken_phrase) is True
        mock_app.send_command.assert_called_once()
        assert mock_app.send_command.call_args.args[0] == {
            "action": "hotkey_action",
            "params": {"keys": keys, "repeat": repeat},
        }

    @pytest.mark.parametrize(("spoken_phrase", "doc_id", "_keys", "_repeat"), NEW_FORMS)
    def test_first_match_is_the_direction_entry(
        self, spoken_phrase, doc_id, _keys, _repeat
    ):
        """No earlier entry (select-phrase, or any command that starts with
        write, right, or left) captures a new form."""
        assert _first_match_doc_id(spoken_phrase) == doc_id

    @pytest.mark.parametrize(("spoken_phrase", "doc_id"), EXISTING_FORMS)
    def test_existing_forms_reach_the_same_entry(self, spoken_phrase, doc_id):
        assert _first_match_doc_id(spoken_phrase) == doc_id


# A5 (David, 2026-09-26 12:19: "Add write to those commands too"): "write"
# also works wherever "right" is a direction. Each row pairs a "write" form
# with the "right" form it must equal, and the entry both must reach.
# (write form, right form, doc_id)
WRITE_DIRECTION_FORMS = [
    ("snap window to the write", "snap window to the right", "snap-window-right"),
    ("snap window to write", "snap window to right", "snap-window-right"),
    ("scroll write", "scroll right", "scroll-right"),
    ("scroll write 3", "scroll right 3", "scroll-right"),
    ("start scrolling write", "start scrolling right", "scroll-start-right"),
    ("go write 3 words", "go right 3 words", "nav-right-words"),
    ("move write word", "move right word", "nav-right-words"),
    ("go write two words", "go right two words", "nav-right-words"),
    ("go write", "go right", "nav-right-times"),
    ("move write 3 times", "move right 3 times", "nav-right-times"),
    ("go write two times", "go right two times", "nav-right-times"),
    ("move slider write", "move slider right", "move-slider-right"),
    ("move slider write 4 times", "move slider right 4 times", "move-slider-right"),
]

# "right" that is not a direction keeps its meaning; nothing new claims
# these phrases.
NOT_A_DIRECTION_FORMS = [
    ("right click", "grid-gesture-click"),
    ("right click save", "click-element-gesture"),
]


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


class TestWriteWhereRightIsADirection:

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("write_form", "right_form", "doc_id"), WRITE_DIRECTION_FORMS)
    async def test_write_form_sends_what_the_right_form_sends(
        self, write_form, right_form, doc_id
    ):
        right_executed, right_commands, right_requests = await _calls_for(right_form)
        assert right_executed is True
        assert right_commands or right_requests, f"{right_form!r} sent nothing"
        assert await _calls_for(write_form) == (True, right_commands, right_requests)

    @pytest.mark.parametrize(("write_form", "right_form", "doc_id"), WRITE_DIRECTION_FORMS)
    def test_first_match_is_the_right_direction_entry(self, write_form, right_form, doc_id):
        assert _first_match_doc_id(right_form) == doc_id
        assert _first_match_doc_id(write_form) == doc_id

    @pytest.mark.parametrize(("spoken_phrase", "doc_id"), NOT_A_DIRECTION_FORMS)
    def test_right_that_is_not_a_direction_is_unchanged(self, spoken_phrase, doc_id):
        assert _first_match_doc_id(spoken_phrase) == doc_id
