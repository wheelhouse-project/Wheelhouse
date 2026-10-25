"""The "record my training word" spoken command (wh-first-sound-lost.23, item 27).

The phrase, said alone, opens the 'Your training word' window. The action
``open_lead_word`` sends {"action": "open_lead_word"} on the Logic-to-GUI
state queue and the GUI process opens the window.

These tests load the real shipped patterns.toml.
"""
import asyncio
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from speech import action_catalog
from speech.actions import ActionFunctions
from speech.pattern_catalog import PatternCatalog

_SERVICE_DIR = Path(__file__).parent.parent
_SYSTEM_PATTERNS = _SERVICE_DIR / "speech" / "config" / "patterns.toml"


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    # Hermetic: a user file that does not exist, so only the shipped file loads.
    user_file = tmp_path_factory.mktemp("lw") / "user_patterns.toml"
    return PatternCatalog(str(_SYSTEM_PATTERNS), user_patterns_file=str(user_file))


def _lead_word_entry(catalog):
    entries = [
        p
        for p in catalog.get_all_patterns()
        if any(a.get("function") == "open_lead_word" for a in p["actions"])
    ]
    assert len(entries) == 1, "expected exactly one open_lead_word command"
    return entries[0]


def _first_match_raw_pattern(catalog, phrase):
    """The raw pattern of the first entry in FILE ORDER that matches."""
    for entry in catalog.get_all_patterns():
        if entry["compiled_pattern"].match(phrase):
            return entry["raw_pattern"]
    return None


def test_the_phrase_alone_resolves_to_the_open_lead_word_command(catalog):
    entry = _lead_word_entry(catalog)
    compiled = entry["compiled_pattern"]

    assert entry["raw_pattern"] == "^record my training word$"
    assert compiled.fullmatch("record my training word")
    assert entry["pattern_type"] == "command"
    assert entry["whole_utterance_only"] is True
    assert entry["requires_hotword"] is False
    assert entry["actions"] == [{"function": "open_lead_word"}]


def test_the_phrase_alone_reaches_the_open_lead_word_row_first(catalog):
    entry = _lead_word_entry(catalog)
    assert _first_match_raw_pattern(catalog, "record my training word") == (
        entry["raw_pattern"]
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "please record my training word now",
        "I will record my training word tomorrow",
        "record my training word for the file",
        "then record my training word",
    ],
)
def test_the_phrase_inside_a_longer_sentence_is_typed_as_text(catalog, sentence):
    entry = _lead_word_entry(catalog)
    assert _first_match_raw_pattern(catalog, sentence) != entry["raw_pattern"]
    assert not entry["compiled_pattern"].match(sentence)


def test_the_row_has_its_own_doc_id_next_to_the_settings_command():
    with _SYSTEM_PATTERNS.open("rb") as handle:
        rows = tomllib.load(handle)["pattern"]
    ids = [row.get("doc_id") for row in rows]
    assert ids.count("record-my-training-word") == 1
    assert ids.index("record-my-training-word") == ids.index("settings-window") + 1


def test_open_lead_word_action_sends_the_gui_open_message():
    handler = MagicMock()
    funcs = ActionFunctions(handler)

    assert "open_lead_word" in funcs.get_functions()
    asyncio.run(funcs.open_lead_word())

    queue = handler.logic_controller.state_manager.state_to_gui_queue
    queue.put_nowait.assert_called_once_with({"action": "open_lead_word"})


def test_the_action_catalog_describes_open_lead_word():
    entries = [e for e in action_catalog.ACTION_CATALOG if e["name"] == "open_lead_word"]
    assert len(entries) == 1
    assert entries[0]["label"] == "Your training word"
    assert entries[0]["group"] == "Wheelhouse"


def test_the_action_catalog_entry_is_complete():
    (entry,) = [
        e for e in action_catalog.ACTION_CATALOG if e["name"] == "open_lead_word"
    ]
    assert entry["params"] == []
    assert entry["audience"] == "advanced"
    assert "Parakeet" in entry["summary"]
    assert entry["example"] == (
        'Trigger "^record my training word$" with no params: saying '
        '"record my training word" opens the training-word window.'
    )


def test_open_lead_word_returns_nothing_and_survives_a_missing_queue():
    handler = MagicMock()
    handler.logic_controller = None
    funcs = ActionFunctions(handler)
    assert asyncio.run(funcs.open_lead_word()) is None


# --------------------------------------------------------------------------
# Item 27 through the speech processor (wh-first-sound-lost.23, stage G2, T4).
# The catalog tests above read the pattern row. These words go through the
# real SpeechProcessor, router and command engine with the shipped
# patterns.toml, so a row that fires early (a lost whole_utterance_only flag)
# shows up as a command that runs, and not as a row that reads right.
# --------------------------------------------------------------------------

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))

from test_speech_pipeline import SpeechPipelineHarness  # noqa: E402

_OPEN_MESSAGE = {"action": "open_lead_word"}


@pytest.fixture
async def pipeline():
    harness = SpeechPipelineHarness(str(_SYSTEM_PATTERNS))
    await harness.start()
    yield harness
    await harness.stop()


def _gui_messages(harness):
    queue = (
        harness.mock_speech_handler.logic_controller.state_manager
        .state_to_gui_queue
    )
    return [call.args[0] for call in queue.put_nowait.call_args_list]


async def _say(harness, sentence):
    words = sentence.split()
    for index, word in enumerate(words):
        await harness.send_word(
            word, start_of_utterance=(index == 0),
            delay_before_ms=0 if index == 0 else 50,
        )
    await harness.send_utterance_end_marker(harness._utterance_counter)
    await harness.wait_for_timeout(300)


@pytest.mark.asyncio
async def test_the_phrase_said_alone_opens_the_window_and_types_nothing(pipeline):
    await _say(pipeline, "record my training word")
    assert _gui_messages(pipeline).count(_OPEN_MESSAGE) == 1
    assert pipeline.get_dictation_texts() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sentence",
    [
        "record my training word now",
        "record my training word for the file",
        "then record my training word",
        "please record my training word now",
    ],
    ids=["phrase-first", "phrase-first-long", "phrase-last", "phrase-middle"],
)
async def test_the_phrase_inside_a_longer_sentence_is_dictated_normally(
    pipeline, sentence
):
    await _say(pipeline, sentence)
    assert _OPEN_MESSAGE not in _gui_messages(pipeline)
    typed = " ".join(pipeline.get_dictation_texts())
    for word in sentence.split():
        assert word in typed, (word, typed)
