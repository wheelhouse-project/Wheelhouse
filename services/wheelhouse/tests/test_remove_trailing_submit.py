"""The trailing-position exception is gone (bead wh-remove-trailing-submit).

David's decision (boss chat 2026-09-26 16:45): commands must start an
utterance, and "submit" at the END of an utterance was the only exception
(the position = "trailing" feature, wh-2vz). The exception is removed.
"submit" still presses Enter, but only as the whole utterance (16:50):
the shipped row now uses whole_utterance_only = true, the same mechanism
the shipped "submitted" row uses (wh-submitted-enter).

Acceptance groups:
  A1  "hello world submit" types all three words and presses no Enter.
  A2  "submit" as the whole utterance still presses Enter.
  A3  The trailing-position code is removed, not disabled.
  A4  A leftover position = "trailing" loads as whole-utterance-only
      (BOSS RULING 02:27 2026-09-27, Q2): the row fires only when its
      pattern matches the whole utterance, and one warning names the
      position field and says so.
  A7  "submit the form" types as text; "submit" and "submitted" as whole
      utterances press Enter with the shipped open-utterance hold ON, at
      the utterance end and at the hold deadline when no end arrives.
"""
import asyncio
import logging
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import pytest

from speech import speech_processor as sp_module
from speech.command_engine import TextParser
from speech.domain import ProcessingMode
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from speech.word_event import WordEvent
from tests.test_speech_pipeline import SpeechPipelineHarness
from tests.test_voice_access_punctuation_router import (
    _hotkey_key_lists,
    _inserted_text,
    _send_phrase,
)

PATTERNS_PATH = wheelhouse_dir / "speech" / "config" / "patterns.toml"


# ============================================================================
# The shipped rows
# ============================================================================

@pytest.fixture(scope="module")
def rows():
    """Every shipped pattern row, in file order, straight from the TOML."""
    return tomllib.loads(PATTERNS_PATH.read_text(encoding="utf-8"))["pattern"]


def _row(rows, doc_id):
    matches = [row for row in rows if row.get("doc_id") == doc_id]
    assert len(matches) == 1, f"expected one row with doc_id {doc_id!r}"
    return matches[0]


class TestShippedRows:
    def test_submit_row_is_whole_utterance_only(self, rows):
        """A2/A7: the submit row is the same shape as the submitted row."""
        row = _row(rows, "submit-enter")
        assert row["pattern"] == "^submit$"
        assert row.get("whole_utterance_only") is True
        assert row["actions"] == [
            {"function": "press_keys", "params": ["enter"]}
        ]

    def test_no_shipped_row_carries_a_position(self, rows):
        """A3: no shipped pattern keeps position = "trailing"."""
        with_position = [
            row.get("doc_id") or row.get("pattern")
            for row in rows if "position" in row
        ]
        assert with_position == []

    def test_patterns_toml_no_longer_describes_the_trailing_submit(self):
        """A5: the header and the "literal submit" note are gone."""
        text = PATTERNS_PATH.read_text(encoding="utf-8")
        assert "Trailing-position" not in text
        assert '"literal submit"' not in text
        assert 'position = "trailing"' not in text


# ============================================================================
# A3: the trailing-position code is removed, not disabled
# ============================================================================

class TestTheCodeIsRemoved:
    @pytest.mark.parametrize(
        "name",
        [
            "get_trailing_command",
            "_build_trailing_entry",
        ],
    )
    def test_catalog_has_no_trailing_support(self, name):
        assert not hasattr(PatternCatalog, name), name

    def test_catalog_instance_has_no_trailing_map(self, tmp_path):
        catalog = PatternCatalog(
            str(PATTERNS_PATH), str(tmp_path / "user_patterns.toml"),
        )
        assert not hasattr(catalog, "trailing_commands")

    @pytest.mark.parametrize(
        "name",
        [
            "_pending_trailing_word",
            "_split_trailing_word_at_marker_finalization",
            "_usable_trailing_command",
            "_maybe_hold_trailing_candidate",
            "_flush_pending_trailing_word_as_dictation",
            "_clear_held_trailing_word",
            "_consume_pending_trailing_word_at_utterance_end",
            "_fire_trailing_action_for_word",
        ],
    )
    def test_processor_has_no_trailing_support(self, name):
        assert not hasattr(SpeechProcessor, name), name

    def test_held_tail_has_no_trailing_kind(self):
        kinds = {kind.name for kind in sp_module._HeldTailKind}
        assert kinds == {"REPLACEMENT_PREFIX", "BARE_NUMBER"}, kinds


# ============================================================================
# A1, A2, A7 through the pipeline harness (hold 0)
# ============================================================================

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
    async def test_hello_world_submit_types_submit_and_presses_no_enter(
        self, harness,
    ):
        """A1: the exact case the bead names."""
        await _send_phrase(harness, ("hello", "world", "submit"))

        assert _hotkey_key_lists(harness) == [], (
            f"'hello world submit' sent {_hotkey_key_lists(harness)!r}"
        )
        dictated = " ".join(_inserted_text(harness))
        for word in ("hello", "world", "submit"):
            assert word in dictated, (
                f"{word!r} was not typed; dictated={dictated!r}"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words",
        [
            ("please", "submit"),
            ("I", "will", "submit"),
            ("then", "press", "submit"),
            ("hello", "submit."),
        ],
    )
    async def test_any_longer_utterance_ending_in_submit_types_the_word(
        self, harness, words,
    ):
        """A1: two or more words ending in "submit" type the word."""
        await _send_phrase(harness, words)

        offending = _non_dictation_actions(harness)
        assert not offending, (
            f"{' '.join(words)!r} ran {offending!r}; the whole utterance "
            "must be ordinary dictation"
        )
        dictated = " ".join(_inserted_text(harness))
        assert "submit" in dictated, (
            f"'submit' was not typed; dictated={dictated!r}"
        )

    @pytest.mark.asyncio
    async def test_submit_the_form_types_as_text(self, harness):
        """A7: "submit" is not the only word, so no Enter."""
        await _send_phrase(harness, ("submit", "the", "form"))

        offending = _non_dictation_actions(harness)
        assert not offending, f"'submit the form' ran {offending!r}"
        dictated = " ".join(_inserted_text(harness))
        for word in ("submit", "the", "form"):
            assert word.lower() in dictated.lower(), (
                f"{word!r} was lost; dictated={dictated!r}"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("word", ["submit", "Submit", "submitted"])
    async def test_the_whole_utterance_presses_enter(self, harness, word):
        """A2: the word alone presses Enter and types nothing."""
        await _send_phrase(harness, (word,))

        assert _hotkey_key_lists(harness) == [["enter"]], (
            f"{word!r} sent {_hotkey_key_lists(harness)!r}"
        )
        assert _inserted_text(harness) == [], (
            f"{word!r} was typed: {_inserted_text(harness)!r}"
        )


# ============================================================================
# A7: the shipped open-utterance hold ON (the processor's default)
# ============================================================================

class RecordingApp:
    """Records every payload the processor sends to the Input process."""

    def __init__(self):
        self.actions: List[Dict[str, Any]] = []

    async def send_command(self, payload: dict):
        self.actions.append(payload)

    async def send_request(self, action: str, params: dict):
        self.actions.append({"action": action, "params": params})
        return {"status": "success"}

    def effects(self) -> List[Dict[str, Any]]:
        return [
            a for a in self.actions
            if a.get("action") not in ("start_utterance", "end_utterance")
        ]

    def inserted(self) -> List[str]:
        return [
            a["params"]["insertion_string"] for a in self.actions
            if a.get("action") == "intelligent_insert_text"
        ]

    def hotkeys(self) -> List[Any]:
        return [
            a["params"].get("keys") for a in self.actions
            if a.get("action") == "hotkey_action"
        ]


def _word(word: str, utterance_id: int, first: bool) -> WordEvent:
    """A real word in the shape integrations/websocket_manager.py builds."""
    return WordEvent(
        word=word,
        start_of_utterance=first,
        end_of_utterance=False,
        utterance_id=utterance_id,
    )


def _end_marker(utterance_id: int) -> WordEvent:
    return WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=True,
        utterance_id=utterance_id,
        is_utterance_end_marker=True,
    )


def _default_hold_processor(monkeypatch, clock, tmp_path):
    """A SpeechProcessor built the way speech_handler.py builds it -- no
    open_utterance_hold_ms argument, so the shipped hold is ON -- with the
    shipped timers and a fake monotonic clock. Returns (app, processor,
    armed timer durations, expire)."""
    monkeypatch.setattr(
        sp_module, "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
    )
    app = RecordingApp()
    catalog = PatternCatalog(
        str(PATTERNS_PATH), str(tmp_path / "user_patterns.toml"),
    )
    handler = MagicMock()
    handler.app = app
    processor = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=TextParser(handler, catalog),
        app=app,
        replacement_timeout_ms=700,
        command_timeout_ms=700,
        greedy_timeout_ms=5000,
    )
    assert processor.open_utterance_hold_ms == 5000
    armed: List[int] = []
    real_start = processor._start_timeout

    def spy(duration_ms):
        armed.append(duration_ms)
        real_start(duration_ms)

    monkeypatch.setattr(processor, "_start_timeout", spy)

    async def expire():
        await processor.process_word_event(
            WordEvent.timeout_finalize(token=processor.timeout_token)
        )

    return app, processor, armed, expire


class TestWholeUtteranceWithTheDefaultHold:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("word", ["submit", "submitted"])
    async def test_enter_at_the_utterance_end_after_the_hold(
        self, monkeypatch, tmp_path, word,
    ):
        """The command timer expires, the hold starts, then the end
        marker arrives: Enter is pressed at the utterance end."""
        clock = [100.0]
        app, processor, armed, expire = _default_hold_processor(
            monkeypatch, clock, tmp_path,
        )
        try:
            await processor.process_word_event(_word(word, 1, True))
            await expire()                          # hold, deadline 105.0
            assert app.effects() == [], app.actions
            assert processor.mode == ProcessingMode.COMMAND_BUFFERING
            assert armed == [700, 5000], armed
            clock[0] = 101.0
            await processor.process_word_event(_end_marker(1))
            assert app.hotkeys() == [["enter"]], app.actions
            assert app.inserted() == [], app.actions
        finally:
            await processor.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("word", ["submit", "submitted"])
    async def test_enter_at_the_utterance_end_without_an_expiry(
        self, monkeypatch, tmp_path, word,
    ):
        clock = [100.0]
        app, processor, _armed, _expire = _default_hold_processor(
            monkeypatch, clock, tmp_path,
        )
        try:
            await processor.process_word_event(_word(word, 1, True))
            await processor.process_word_event(_end_marker(1))
            assert app.hotkeys() == [["enter"]], app.actions
            assert app.inserted() == [], app.actions
        finally:
            await processor.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("word", ["submit", "submitted"])
    async def test_enter_at_the_hold_deadline_when_no_end_arrives(
        self, monkeypatch, tmp_path, word,
    ):
        """The final never arrives: nothing happens during the hold, and
        Enter is pressed at the deadline."""
        clock = [100.0]
        app, processor, armed, expire = _default_hold_processor(
            monkeypatch, clock, tmp_path,
        )
        try:
            await processor.process_word_event(_word(word, 1, True))
            await expire()                          # hold, deadline 105.0
            assert app.effects() == [], app.actions
            assert processor.mode == ProcessingMode.COMMAND_BUFFERING
            assert armed == [700, 5000], armed
            # The re-armed timer's own expiry lands at the deadline.
            clock[0] = 105.0
            await expire()
            assert app.hotkeys() == [["enter"]], app.actions
            assert app.inserted() == [], app.actions
            assert processor.mode == ProcessingMode.IDLE
        finally:
            await processor.stop()

    @pytest.mark.asyncio
    async def test_submit_the_form_types_as_text_with_the_hold(
        self, monkeypatch, tmp_path,
    ):
        clock = [100.0]
        app, processor, _armed, expire = _default_hold_processor(
            monkeypatch, clock, tmp_path,
        )
        try:
            await processor.process_word_event(_word("submit", 1, True))
            await expire()                          # hold
            clock[0] = 101.0
            await processor.process_word_event(_word("the", 1, False))
            await processor.process_word_event(_word("form", 1, False))
            await processor.process_word_event(_end_marker(1))
            assert app.hotkeys() == [], app.actions
            typed = " ".join(app.inserted()).lower()
            for spoken in ("submit", "the", "form"):
                assert spoken in typed, app.actions
        finally:
            await processor.stop()


# ============================================================================
# A4 (BOSS RULING 02:27 2026-09-27, Q2): a leftover position = "trailing"
# loads as whole-utterance-only, with one warning
# ============================================================================
#
# The pattern editor carries an entry's position key through a Customize
# save (create_pattern_dialog.py get_pattern_data), so a user who edited the
# built-in submit row before this change can hold a copy of the OLD row:
# the unanchored 'submit' with position = "trailing". Loaded as an ordinary
# unanchored pattern it would press Enter wherever "submit" was spoken, in
# the middle of dictation. The ruling: such a row matches only the whole
# utterance.

_HEADER = 'COMMAND_HOTWORD = "x-ray"\n\n'

_ROW = """
[[pattern]]
pattern = '''{pattern}'''
{extra}actions = [
    {{ function = "press_keys", params = ["f5"] }}
]
"""

# The shipped row as it stood at eedb0a4c (patterns.toml lines 103-109),
# the row a Customize save copied.
_OLD_SUBMIT_ROW = """
[[pattern]]
pattern = '''submit'''
doc_id = "submit-enter"
position = "trailing"
actions = [
    { function = "press_keys", params = ["enter"] }
]
"""

_TRAILING = 'position = "trailing"\n'


def _write(path: Path, body: str) -> str:
    path.write_text(body, encoding="utf-8")
    return str(path)


def _row_text(pattern: str = "^deploy$", extra: str = "") -> str:
    return _ROW.format(pattern=pattern, extra=extra)


def _comparable(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The built entry minus the fields that name the source file."""
    return {
        key: (value.pattern if key == "compiled_pattern" else value)
        for key, value in entry.items()
        if key not in ("is_user", "literal_prefix_matchers",
                       "literal_body_matchers")
    }


def _position_warnings(caplog) -> List[logging.LogRecord]:
    return [
        record for record in caplog.records
        if record.levelno == logging.WARNING
        and "position" in record.getMessage()
    ]


def _only_row(catalog: PatternCatalog) -> Dict[str, Any]:
    entries = catalog.get_all_patterns()
    assert len(entries) == 1, entries
    return entries[0]


class TestLeftoverTrailingPosition:
    def test_anchored_row_loads_as_the_whole_utterance_only_row(
        self, tmp_path,
    ):
        """position = "trailing" on a ^-anchored row builds exactly the
        row that says whole_utterance_only = true instead."""
        with_field = PatternCatalog(
            _write(tmp_path / "a.toml", _HEADER + _row_text(extra=_TRAILING)),
            str(tmp_path / "no_user.toml"),
        )
        whole_utterance = PatternCatalog(
            _write(tmp_path / "b.toml", _HEADER + _row_text(
                extra="whole_utterance_only = true\n",
            )),
            str(tmp_path / "no_user.toml"),
        )
        assert with_field.pattern_count == whole_utterance.pattern_count == 1
        assert [_comparable(e) for e in with_field.get_all_patterns()] == [
            _comparable(e) for e in whole_utterance.get_all_patterns()
        ]
        assert _only_row(with_field)["whole_utterance_only"] is True
        assert sorted(with_field.first_words) == sorted(
            whole_utterance.first_words
        )

    def test_unanchored_row_becomes_a_whole_utterance_command(self, tmp_path):
        """The old row was unanchored. whole_utterance_only is honoured
        only on a command, so the loader anchors the row: it matches the
        whole utterance and nothing else. The raw expression is kept, so
        the row keeps its identity in the Pattern Manager and the try-it
        preview."""
        catalog = PatternCatalog(
            _write(tmp_path / "a.toml",
                   _HEADER + _row_text(pattern="deploy", extra=_TRAILING)),
            str(tmp_path / "no_user.toml"),
        )
        row = _only_row(catalog)
        assert row["pattern_type"] == "command"
        assert row["whole_utterance_only"] is True
        assert row["raw_pattern"] == "deploy"
        compiled = row["compiled_pattern"]
        assert compiled.pattern.startswith("^")
        assert compiled.fullmatch("deploy")
        assert compiled.fullmatch("Deploy")
        assert not compiled.fullmatch("deploy now")
        assert not compiled.search("please deploy now")
        assert [
            ptype for _, ptype, _ in catalog.get_matching_patterns("deploy")
        ] == ["command"]

    def test_one_warning_names_the_field_and_says_whole_utterance(
        self, tmp_path, caplog,
    ):
        caplog.set_level(logging.WARNING)
        PatternCatalog(
            _write(tmp_path / "a.toml",
                   _HEADER + _row_text(pattern="deploy", extra=_TRAILING)),
            str(tmp_path / "no_user.toml"),
        )
        warnings = _position_warnings(caplog)
        assert len(warnings) == 1, [w.getMessage() for w in warnings]
        message = warnings[0].getMessage()
        assert "position" in message, message
        assert "whole utterance" in message, message
        assert "'deploy'" in message, message
        # No other warning of any kind for the row: anchoring it keeps the
        # "whole_utterance_only is only supported on commands" warning off.
        others = [
            r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and r not in warnings
        ]
        assert others == [], others

    def test_no_warning_and_no_change_without_the_field_or_for_leading(
        self, tmp_path, caplog,
    ):
        caplog.set_level(logging.WARNING)
        absent = PatternCatalog(
            _write(tmp_path / "a.toml", _HEADER + _row_text()),
            str(tmp_path / "no_user.toml"),
        )
        leading = PatternCatalog(
            _write(tmp_path / "b.toml", _HEADER + _row_text(
                extra='position = "leading"\n',
            )),
            str(tmp_path / "no_user.toml"),
        )
        unanchored_leading = PatternCatalog(
            _write(tmp_path / "c.toml", _HEADER + _row_text(
                pattern="deploy", extra='position = "leading"\n',
            )),
            str(tmp_path / "no_user.toml"),
        )
        assert _position_warnings(caplog) == []
        assert _only_row(absent)["whole_utterance_only"] is False
        assert [_comparable(e) for e in leading.get_all_patterns()] == [
            _comparable(e) for e in absent.get_all_patterns()
        ]
        row = _only_row(unanchored_leading)
        assert row["pattern_type"] == "replacement"
        assert row["whole_utterance_only"] is False
        assert row["compiled_pattern"].pattern == "deploy"

    def test_user_row_loads_whole_utterance_only_and_warns_once(
        self, tmp_path, caplog,
    ):
        caplog.set_level(logging.WARNING)
        user_file = _write(
            tmp_path / "user_patterns.toml", _row_text(extra=_TRAILING),
        )
        catalog = PatternCatalog(str(PATTERNS_PATH), user_file)
        user_rows = [
            entry for entry in catalog.get_all_patterns()
            if entry.get("raw_pattern") == "^deploy$"
        ]
        assert len(user_rows) == 1
        assert user_rows[0]["is_user"] is True
        assert user_rows[0]["whole_utterance_only"] is True
        assert "deploy" in catalog.first_words
        warnings = _position_warnings(caplog)
        assert len(warnings) == 1, [w.getMessage() for w in warnings]
        assert user_file in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_user_row_fires_only_as_the_whole_utterance(
        self, monkeypatch, tmp_path,
    ):
        """Said alone, the leftover row presses its key, through the
        processor with the default hold."""
        _write(tmp_path / "user_patterns.toml", _row_text(extra=_TRAILING))
        clock = [100.0]
        app, processor, _armed, _expire = _default_hold_processor(
            monkeypatch, clock, tmp_path,
        )
        try:
            await processor.process_word_event(_word("deploy", 1, True))
            await processor.process_word_event(_end_marker(1))
            assert app.hotkeys() == [["f5"]], app.actions
            assert app.inserted() == [], app.actions
        finally:
            await processor.stop()


async def _speak(processor, words, utterance_id=1):
    for index, word in enumerate(words):
        await processor.process_word_event(
            _word(word, utterance_id, index == 0)
        )
    await processor.process_word_event(_end_marker(utterance_id))


class TestUserCopyOfTheOldSubmitRow:
    """A user_patterns.toml row that copies the old shipped row replaces
    the shipped submit row (same doc_id) and follows the whole-utterance
    rule, with the shipped open-utterance hold ON."""

    @pytest.fixture
    def speak_with_old_row(self, monkeypatch, tmp_path):
        _write(tmp_path / "user_patterns.toml", _OLD_SUBMIT_ROW)
        clock = [100.0]
        app, processor, _armed, _expire = _default_hold_processor(
            monkeypatch, clock, tmp_path,
        )
        # The copy claims the built-in: exactly one submit rule is live,
        # and it is the user's.
        submit_rows = [
            entry for entry in processor.catalog.get_all_patterns()
            if entry.get("doc_id") == "submit-enter"
        ]
        assert [(e["raw_pattern"], e["is_user"]) for e in submit_rows] == [
            ("submit", True),
        ]

        async def speak(words):
            try:
                await _speak(processor, words)
            finally:
                await processor.stop()
            return app

        return speak

    @pytest.mark.asyncio
    async def test_submit_the_form_types_text_and_presses_no_enter(
        self, speak_with_old_row,
    ):
        app = await speak_with_old_row(("submit", "the", "form"))
        assert app.hotkeys() == [], app.actions
        typed = " ".join(app.inserted()).lower()
        for spoken in ("submit", "the", "form"):
            assert spoken in typed, app.actions

    @pytest.mark.asyncio
    async def test_submit_alone_presses_enter(self, speak_with_old_row):
        app = await speak_with_old_row(("submit",))
        assert app.hotkeys() == [["enter"]], app.actions
        assert app.inserted() == [], app.actions

    @pytest.mark.asyncio
    async def test_hello_world_submit_types_text(self, speak_with_old_row):
        app = await speak_with_old_row(("hello", "world", "submit"))
        assert app.hotkeys() == [], app.actions
        typed = " ".join(app.inserted()).lower()
        for spoken in ("hello", "world", "submit"):
            assert spoken in typed, app.actions
