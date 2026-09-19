"""Test fragment for wh-voice-access-parity.1.3 (delete/cut/copy range
commands). STAGING FRAGMENT -- not wired into the tests/ tree yet. Placing
it (and the companion pattern fragment wh-voice-access-parity.1.3.toml into
services/wheelhouse/speech/config/patterns.toml) is the boss's job.

These tests assert against the PRODUCTION pattern file,
services/wheelhouse/speech/config/patterns.toml, using the same technique as
the wh-voice-access-parity.1.1 tests that used to live in the repo-root
tests/speech suite (_production_pattern_entries / _first_file_order_match),
and the same exact-actions-list style as that suite's
test_delete_all_selects_everything_then_deletes and
test_click_element_entry_requires_the_hotword. That suite was retired under
wh-speech-suite-repair-or-retire; the technique it established lives on here.

EXPECTED TO FAIL until the fragment above is merged into patterns.toml: none
of the doc_ids below exist there yet, so every _first_file_order_match(...)
lookup returns None and every _production_pattern_entries() doc_id search
comes up empty. That is correct, not a bug in these tests -- do not add the
patterns to patterns.toml to make them pass; that placement is the boss's
call (see the fragment's header comment for the ordering-hazard and
duplicate-check evidence).

Covers every SCOPE form in wh-voice-access-parity.1.3:
  - 36 range forms (delete/cut/copy x next/previous/last x
    character/word/line/paragraph), each checked with both an unnumbered
    trigger ("delete next word") and a numbered one ("delete next 3 words").
  - delete this word / delete (this) line / delete (this) paragraph (Group C).
Duplicates NOT re-authored ("delete all" -> doc_id delete-all, "delete word"
-> doc_id delete-word) are documented in the fragment header and this
session's return, not re-tested here, per the 1:1 "one pattern block, one
test, per authored SCOPE form" stopping condition -- these two forms have no
new pattern block in the fragment, so they get no new test here either.
"""

import re
import sys
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

project_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(project_root))

from services.wheelhouse.speech.pattern_catalog import PatternCatalog
from services.wheelhouse.speech.command_engine import TextParser


# ============================================================================
# FIXTURES (kept self-contained so this fragment runs standalone regardless
# of where it lands)
# ============================================================================

@pytest.fixture
def catalog():
    """Real pattern catalog with production patterns."""
    patterns_file = str(
        project_root / "services" / "wheelhouse" / "speech" / "config" / "patterns.toml"
    )
    return PatternCatalog(patterns_file)


@pytest.fixture
def mock_app():
    """Mock app that captures commands."""
    app = Mock()
    app.send_command = AsyncMock()
    app.send_request = AsyncMock(return_value={"status": "success"})
    return app


@pytest.fixture
def parser(catalog, mock_app):
    """TextParser with production patterns and mock app.

    parse_and_execute is wrapped to pass authorized_command=True: these
    tests simulate command-mode text that in production has already passed
    the router's hotword gate (wh-qj70s / wh-z69w).
    """
    mock_handler = Mock()
    mock_handler.app = mock_app
    p = TextParser(mock_handler, catalog)
    orig = p.parse_and_execute
    p.parse_and_execute = (
        lambda text, **kw: orig(text, **{"authorized_command": True, **kw})
    )
    return p


def _production_pattern_entries():
    """Load the authored pattern entries, including their stable doc IDs."""
    patterns_file = (
        project_root
        / "services"
        / "wheelhouse"
        / "speech"
        / "config"
        / "patterns.toml"
    )
    with patterns_file.open("rb") as handle:
        return tomllib.load(handle)["pattern"]


def _first_file_order_match(spoken_text):
    """The entry a spoken phrase resolves to under first-match-wins."""
    return next(
        (
            entry
            for entry in _production_pattern_entries()
            if re.compile(entry["pattern"], re.IGNORECASE).search(spoken_text)
        ),
        None,
    )


def _entry_for_doc_id(doc_id):
    return next(
        (e for e in _production_pattern_entries() if e.get("doc_id") == doc_id),
        None,
    )


# ============================================================================
# DATA: every range form (verb x position x unit), with an unnumbered and a
# numbered trigger phrase, and the exact expected actions list. Generated
# directly from patterns-staging/wh-voice-access-parity.1.3.toml so the table
# cannot drift from the fragment by hand-transcription error.
# ============================================================================

RANGE_FORM_CASES = (
    ("delete next character", "delete next 3 characters", "delete-next-character", [
        {"function": "hk", "params": ["shift", "right", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    ("delete next word", "delete next 3 words", "delete-next-word", [
        {"function": "hk", "params": ["shift", "ctrl", "right", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    ("delete next line", "delete next 3 lines", "delete-next-line", [
        {"function": "hk", "params": ["home"], "awaits_done": True},
        {"function": "hk", "params": ["shift", "down", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    ("delete next paragraph", "delete next 3 paragraphs", "delete-next-paragraph", [
        {"function": "hk", "params": ["shift", "ctrl", "down", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    ("delete previous character", "delete previous 3 characters", "delete-previous-character", [
        {"function": "hk", "params": ["shift", "left", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    ("delete previous word", "delete previous 3 words", "delete-previous-word", [
        {"function": "hk", "params": ["shift", "ctrl", "left", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    ("delete previous line", "delete previous 3 lines", "delete-previous-line", [
        {"function": "hk", "params": ["home"], "awaits_done": True},
        {"function": "hk", "params": ["shift", "up", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    ("delete previous paragraph", "delete previous 3 paragraphs", "delete-previous-paragraph", [
        {"function": "hk", "params": ["shift", "ctrl", "up", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    ("delete last character", "delete last 3 characters", "delete-last-character", [
        {"function": "hk", "params": ["shift", "left", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    ("delete last word", "delete last 3 words", "delete-last-word", [
        {"function": "hk", "params": ["shift", "ctrl", "left", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    ("delete last line", "delete last 3 lines", "delete-last-line", [
        {"function": "hk", "params": ["home"], "awaits_done": True},
        {"function": "hk", "params": ["shift", "up", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    ("delete last paragraph", "delete last 3 paragraphs", "delete-last-paragraph", [
        {"function": "hk", "params": ["shift", "ctrl", "up", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    ("cut next character", "cut next 3 characters", "cut-next-character", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "right", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "x"], "awaits_done": True},
    ]),
    ("cut next word", "cut next 3 words", "cut-next-word", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "right", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "x"], "awaits_done": True},
    ]),
    ("cut next line", "cut next 3 lines", "cut-next-line", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["home"], "awaits_done": True},
        {"function": "hk", "params": ["shift", "down", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "x"], "awaits_done": True},
    ]),
    ("cut next paragraph", "cut next 3 paragraphs", "cut-next-paragraph", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "down", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "x"], "awaits_done": True},
    ]),
    ("cut previous character", "cut previous 3 characters", "cut-previous-character", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "left", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "x"], "awaits_done": True},
    ]),
    ("cut previous word", "cut previous 3 words", "cut-previous-word", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "left", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "x"], "awaits_done": True},
    ]),
    ("cut previous line", "cut previous 3 lines", "cut-previous-line", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["home"], "awaits_done": True},
        {"function": "hk", "params": ["shift", "up", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "x"], "awaits_done": True},
    ]),
    ("cut previous paragraph", "cut previous 3 paragraphs", "cut-previous-paragraph", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "up", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "x"], "awaits_done": True},
    ]),
    ("cut last character", "cut last 3 characters", "cut-last-character", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "left", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "x"], "awaits_done": True},
    ]),
    ("cut last word", "cut last 3 words", "cut-last-word", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "left", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "x"], "awaits_done": True},
    ]),
    ("cut last line", "cut last 3 lines", "cut-last-line", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["home"], "awaits_done": True},
        {"function": "hk", "params": ["shift", "up", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "x"], "awaits_done": True},
    ]),
    ("cut last paragraph", "cut last 3 paragraphs", "cut-last-paragraph", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "up", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "x"], "awaits_done": True},
    ]),
    ("copy next character", "copy next 3 characters", "copy-next-character", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "right", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
    ]),
    ("copy next word", "copy next 3 words", "copy-next-word", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "right", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
    ]),
    ("copy next line", "copy next 3 lines", "copy-next-line", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["home"], "awaits_done": True},
        {"function": "hk", "params": ["shift", "down", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
    ]),
    ("copy next paragraph", "copy next 3 paragraphs", "copy-next-paragraph", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "down", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
    ]),
    ("copy previous character", "copy previous 3 characters", "copy-previous-character", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "left", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
    ]),
    ("copy previous word", "copy previous 3 words", "copy-previous-word", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "left", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
    ]),
    ("copy previous line", "copy previous 3 lines", "copy-previous-line", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["home"], "awaits_done": True},
        {"function": "hk", "params": ["shift", "up", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
    ]),
    ("copy previous paragraph", "copy previous 3 paragraphs", "copy-previous-paragraph", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "up", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
    ]),
    ("copy last character", "copy last 3 characters", "copy-last-character", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "left", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
    ]),
    ("copy last word", "copy last 3 words", "copy-last-word", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "left", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
    ]),
    ("copy last line", "copy last 3 lines", "copy-last-line", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["home"], "awaits_done": True},
        {"function": "hk", "params": ["shift", "up", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
    ]),
    ("copy last paragraph", "copy last 3 paragraphs", "copy-last-paragraph", [
        {"function": "skip_clipboard_restore", "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "up", "g1"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
    ]),
)


# Group C: single-current-unit deletes plus their "this" aliases. No repeat
# count, matching the existing (pre-epic) delete-word entry's shape.
GROUP_C_CASES = (
    (("delete this word",), "delete-this-word", [
        {"function": "hk", "params": ["ctrl", "left"], "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "right"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    (("delete line", "delete this line"), "delete-line", [
        {"function": "hk", "params": ["home"], "awaits_done": True},
        {"function": "hk", "params": ["shift", "end"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
    (("delete paragraph", "delete this paragraph"), "delete-paragraph", [
        {"function": "hk", "params": ["ctrl", "down"], "awaits_done": True},
        {"function": "hk", "params": ["ctrl", "up"], "awaits_done": True},
        {"function": "hk", "params": ["shift", "ctrl", "down"], "awaits_done": True},
        {"function": "hk", "params": ["del"], "awaits_done": True},
    ]),
)


# ============================================================================
# TESTS: every SCOPE form resolves to its own entry with the exact actions
# ============================================================================

class TestRangeFormsResolveToExpectedEntry:
    """Each of the 36 delete/cut/copy x next/previous/last x unit forms
    fires its own doc_id, both unnumbered ("delete next word", implicit
    count 1) and numbered ("delete next 3 words")."""

    @pytest.mark.parametrize(
        ("bare_phrase", "numbered_phrase", "doc_id", "expected_actions"),
        RANGE_FORM_CASES,
    )
    def test_bare_phrase_resolves_to_doc_id(
        self, bare_phrase, numbered_phrase, doc_id, expected_actions
    ):
        first_match = _first_file_order_match(bare_phrase)
        assert first_match is not None, f"nothing matches {bare_phrase!r}"
        assert first_match.get("doc_id") == doc_id, (
            f"{bare_phrase!r} first matched {first_match.get('doc_id')!r}, "
            f"not {doc_id!r}"
        )

    @pytest.mark.parametrize(
        ("bare_phrase", "numbered_phrase", "doc_id", "expected_actions"),
        RANGE_FORM_CASES,
    )
    def test_numbered_phrase_resolves_to_doc_id(
        self, bare_phrase, numbered_phrase, doc_id, expected_actions
    ):
        first_match = _first_file_order_match(numbered_phrase)
        assert first_match is not None, f"nothing matches {numbered_phrase!r}"
        assert first_match.get("doc_id") == doc_id, (
            f"{numbered_phrase!r} first matched {first_match.get('doc_id')!r}, "
            f"not {doc_id!r}"
        )

    @pytest.mark.parametrize(
        ("bare_phrase", "numbered_phrase", "doc_id", "expected_actions"),
        RANGE_FORM_CASES,
    )
    def test_entry_has_exact_expected_actions(
        self, bare_phrase, numbered_phrase, doc_id, expected_actions
    ):
        entry = _entry_for_doc_id(doc_id)
        assert entry is not None, f"no {doc_id} entry"
        assert entry["actions"] == expected_actions


class TestGroupCFormsResolveToExpectedEntry:
    """delete this word / delete (this) line / delete (this) paragraph."""

    @pytest.mark.parametrize(
        ("phrases", "doc_id", "expected_actions"), GROUP_C_CASES
    )
    def test_every_phrase_resolves_to_doc_id(self, phrases, doc_id, expected_actions):
        for phrase in phrases:
            first_match = _first_file_order_match(phrase)
            assert first_match is not None, f"nothing matches {phrase!r}"
            assert first_match.get("doc_id") == doc_id, (
                f"{phrase!r} first matched {first_match.get('doc_id')!r}, "
                f"not {doc_id!r}"
            )

    @pytest.mark.parametrize(
        ("phrases", "doc_id", "expected_actions"), GROUP_C_CASES
    )
    def test_entry_has_exact_expected_actions(self, phrases, doc_id, expected_actions):
        entry = _entry_for_doc_id(doc_id)
        assert entry is not None, f"no {doc_id} entry"
        assert entry["actions"] == expected_actions


# ============================================================================
# TESTS: the two load-bearing mechanism rules, stated as their own guards
# (redundant with the exact-actions checks above by construction, but named
# so a future edit that breaks just the rule -- not the literal key list --
# is easy to diagnose).
# ============================================================================

class TestMechanismRules:
    @pytest.mark.parametrize(
        "doc_id",
        [c[2] for c in RANGE_FORM_CASES if c[2].startswith(("cut-", "copy-"))],
    )
    def test_cut_and_copy_start_with_skip_clipboard_restore(self, doc_id):
        entry = _entry_for_doc_id(doc_id)
        assert entry is not None, f"no {doc_id} entry"
        first_step = entry["actions"][0]
        assert first_step.get("function") == "skip_clipboard_restore", (
            f"{doc_id} must start with skip_clipboard_restore or the "
            f"clipboard is discarded when the utterance ends; got {first_step}"
        )
        assert first_step.get("awaits_done") is True

    @pytest.mark.parametrize(
        "doc_id", [c[2] for c in RANGE_FORM_CASES if c[2].endswith("-line")]
    )
    def test_line_forms_press_home_first(self, doc_id):
        entry = _entry_for_doc_id(doc_id)
        assert entry is not None, f"no {doc_id} entry"
        actions = entry["actions"]
        idx = 1 if actions[0].get("function") == "skip_clipboard_restore" else 0
        home_step = actions[idx]
        assert home_step == {"function": "hk", "params": ["home"], "awaits_done": True}, (
            f"{doc_id} must press Home before extending the selection, so "
            f"the range acts on whole lines the way Voice Access does; "
            f"got {home_step}"
        )

    def test_delete_line_group_c_presses_home_first(self):
        entry = _entry_for_doc_id("delete-line")
        assert entry is not None
        assert entry["actions"][0] == {
            "function": "hk", "params": ["home"], "awaits_done": True,
        }

    @pytest.mark.parametrize(
        "doc_id", [c[2] for c in RANGE_FORM_CASES]
    )
    def test_repeat_count_is_a_real_capture_group_last_in_hk_params(self, doc_id):
        """Lexer rule (epic wh-voice-access-parity): the repeat count must be
        a REAL capture group whose whole body is \\d+, with its gN slot the
        LAST argument to hk. A bare \\d+ or (?:\\d+) gets no spoken-number
        support."""
        entry = _entry_for_doc_id(doc_id)
        assert entry is not None, f"no {doc_id} entry"
        pattern_text = entry["pattern"]
        # A real (unescaped, non-capturing-marker) capturing group whose
        # whole body is \d+.
        assert re.search(r"(?<!\?:)\(\\d\+\)", pattern_text), (
            f"{doc_id} pattern {pattern_text!r} has no real (\\d+) capture "
            f"group"
        )
        g1_steps = [
            a for a in entry["actions"]
            if a.get("params") and a["params"][-1] == "g1"
        ]
        assert len(g1_steps) == 1, (
            f"{doc_id} must have exactly one action step whose params end "
            f"in g1 (the repeat count); found {len(g1_steps)} in "
            f"{entry['actions']}"
        )

    @pytest.mark.parametrize(
        "doc_id", [c[2] for c in RANGE_FORM_CASES] + [c[1] for c in GROUP_C_CASES]
    )
    def test_every_step_awaits_done(self, doc_id):
        entry = _entry_for_doc_id(doc_id)
        assert entry is not None, f"no {doc_id} entry"
        for step in entry["actions"]:
            assert step.get("awaits_done") is True, (
                f"{doc_id} step {step} must set awaits_done=true so the "
                f"next step in the sequence waits for it to complete"
            )


# ============================================================================
# TESTS: end-to-end spoken-number-word support through the real parser, for
# a representative sample (the epic's own repeat-count claim covers all hk
# patterns generically -- see wh-voice-access-parity.1 comment 2026-08-17;
# this exercises it against these specific new entries once merged).
# ============================================================================

class TestEndToEndSpokenNumberSupport:
    @staticmethod
    def _hotkey_repeats(mock_app):
        """Repeat counts of every hotkey_action the parser dispatched.

        Every step in this fragment sets awaits_done (MECHANISM RULE), and
        awaits_done steps go through send_request, never send_command --
        asserting on send_command here would pass only for a fragment that
        violated its own mechanism rule.
        """
        return [
            call.args[1].get("repeat")
            for call in mock_app.send_request.await_args_list
            if call.args[0] == "hotkey_action"
        ]

    @pytest.mark.asyncio
    async def test_delete_next_three_words_spoken_number(self, parser, mock_app):
        result = await parser.parse_and_execute("delete next three words")
        assert result is True
        assert 3 in self._hotkey_repeats(mock_app)

    @pytest.mark.asyncio
    async def test_cut_previous_two_lines_spoken_number(self, parser, mock_app):
        result = await parser.parse_and_execute("cut previous two lines")
        assert result is True
        assert 2 in self._hotkey_repeats(mock_app)

    @pytest.mark.asyncio
    async def test_copy_last_four_characters_spoken_number(self, parser, mock_app):
        result = await parser.parse_and_execute("copy last four characters")
        assert result is True
        assert 4 in self._hotkey_repeats(mock_app)

    @pytest.mark.asyncio
    async def test_delete_this_word_executes(self, parser, mock_app):
        result = await parser.parse_and_execute("delete this word")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_this_line_executes(self, parser, mock_app):
        result = await parser.parse_and_execute("delete this line")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_this_paragraph_executes(self, parser, mock_app):
        result = await parser.parse_and_execute("delete this paragraph")
        assert result is True


# ============================================================================
# TESTS: uniqueness -- these new doc_ids must not collide with each other or
# with any existing doc_id once merged.
# ============================================================================

class TestDocIdsStayUnique:
    def test_all_pattern_doc_ids_are_unique(self):
        doc_ids = [entry.get("doc_id") for entry in _production_pattern_entries()]
        seen = set()
        dupes = set()
        for d in doc_ids:
            if d in seen:
                dupes.add(d)
            seen.add(d)
        assert not dupes, f"duplicate doc_id(s) in patterns.toml: {dupes}"
