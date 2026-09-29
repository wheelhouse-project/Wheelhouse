"""The whole utterance "submitted" presses Enter (bead wh-submitted-enter).

With the Parakeet lead phrase on, the spoken word "submit" was heard as
"submitted" (wh-vad-lead-phrase-trial.3) and was typed as text. David
decided on 2026-09-26 that "submitted" ships as a system command that
presses Enter, but ONLY as the whole utterance, so "the form was
submitted" stays dictation. (The trailing-position "submit" this module
first contrasted with was removed by wh-remove-trailing-submit.)

The row is tied down in the TOML (placement, flag, action), then the
pipeline harness proves the spoken result against the shipped
patterns.toml, including that the existing "submit" behavior is unchanged.
"""
import sys
import tomllib
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from tests.test_speech_pipeline import SpeechPipelineHarness
from tests.test_voice_access_punctuation_router import (
    _hotkey_key_lists,
    _inserted_text,
    _send_phrase,
)

_PATTERNS_PATH = Path(__file__).parent.parent / "speech" / "config" / "patterns.toml"


@pytest.fixture(scope="module")
def rows():
    """Every shipped pattern row, in file order, straight from the TOML."""
    return tomllib.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))["pattern"]


def _index_of(rows, doc_id):
    for index, row in enumerate(rows):
        if row.get("doc_id") == doc_id:
            return index
    return None


class TestSubmittedRow:
    def test_the_row_exists_with_the_whole_utterance_flag(self, rows):
        index = _index_of(rows, "submitted-enter")
        assert index is not None, "no shipped row has doc_id 'submitted-enter'"
        row = rows[index]
        assert row["pattern"] == "^submitted$"
        assert row.get("whole_utterance_only") is True
        assert row["actions"] == [
            {"function": "press_keys", "params": ["enter"]}
        ]
        assert "position" not in row, (
            "the row must not carry the retired position field"
        )

    def test_the_row_sits_next_to_submit_enter(self, rows):
        submit_index = _index_of(rows, "submit-enter")
        assert submit_index is not None, "no shipped row has doc_id 'submit-enter'"
        assert _index_of(rows, "submitted-enter") == submit_index + 1


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
    async def test_submitted_alone_presses_enter(self, harness):
        await _send_phrase(harness, ("submitted",))

        assert _hotkey_key_lists(harness) == [["enter"]], (
            f"'submitted' sent {_hotkey_key_lists(harness)!r}"
        )
        assert _inserted_text(harness) == [], (
            f"'submitted' was typed: {_inserted_text(harness)!r}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words",
        [
            ("the", "form", "was", "submitted"),
            ("submitted", "it"),
            ("I", "submitted"),
            ("hello", "world", "submitted"),
        ],
    )
    async def test_submitted_inside_a_sentence_is_dictation(self, harness, words):
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
    async def test_submit_alone_still_presses_enter(self, harness):
        await _send_phrase(harness, ("submit",))

        assert _hotkey_key_lists(harness) == [["enter"]]
        assert _inserted_text(harness) == []
