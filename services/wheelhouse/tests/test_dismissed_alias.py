"""wh-dismissed-alias: "dismissed" is accepted wherever "dismiss" is a spoken command.

David, 2026-11-06 15:16: "The dismiss command is often transcribed as
dismissed. We should accept both forms everywhere."

Each "dismissed" form must reach the SAME shipped row as its "dismiss" form,
so it runs the same actions. The row is found from the TOML data, the same
way test_voice_access_alias_completeness.py does it: the runtime match result
carries no doc_id. The live matcher and the router are then asked directly,
so the test also proves that the row fires on the whole utterance and that a
longer sentence that starts with "dismissed" stays typed text.
"""
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

_PATTERNS_PATH = Path(__file__).parent.parent / "speech" / "config" / "patterns.toml"

# (the "dismiss" form that ships today, its "dismissed" form, the doc_id of
# the row that owns both)
_PAIRS = [
    ("dismiss", "dismissed", "escape"),
    ("dismiss numbers", "dismissed numbers", "hide-numbers"),
    ("dismiss grid", "dismissed grid", "hide-grid"),
]

# Longer sentences that start with "dismissed" and are not a command. Each one
# mirrors a "dismiss" sentence that the shipped tests already require to type.
_SENTENCES = [
    "dismissed the meeting",
    "dismissed the meeting invite",
    "dismissed numbers from the summary",
    "dismissed grid overlays for now",
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


@pytest.fixture(scope="module")
def router():
    from speech.router import SpeechRouter

    return SpeechRouter(
        PatternCatalog(str(_PATTERNS_PATH), user_patterns_file=""),
        hotword="x-ray",
    )


def _first_row_that_fullmatches(rows, form):
    """(index, row) of the first anchored shipped row whose regex owns `form`."""
    for index, row in enumerate(rows):
        source = row["pattern"]
        if not source.startswith("^"):
            continue
        if re.compile(source, re.IGNORECASE).fullmatch(form):
            return index, row
    return None


class TestDismissedReachesTheSameRowAsDismiss:
    @pytest.mark.parametrize("dismiss,dismissed,doc_id", _PAIRS)
    def test_the_same_row_owns_both_forms(self, rows, dismiss, dismissed, doc_id):
        owner_of_dismiss = _first_row_that_fullmatches(rows, dismiss)
        assert owner_of_dismiss is not None
        assert owner_of_dismiss[1].get("doc_id") == doc_id
        owner_of_dismissed = _first_row_that_fullmatches(rows, dismissed)
        assert owner_of_dismissed is not None, (
            f"no shipped pattern row fullmatches {dismissed!r}; "
            f"{dismiss!r} reaches doc_id {doc_id!r}"
        )
        assert owner_of_dismissed[0] == owner_of_dismiss[0], (
            f"{dismissed!r} reaches row {owner_of_dismissed[0]} "
            f"(doc_id {owner_of_dismissed[1].get('doc_id')!r}), but "
            f"{dismiss!r} reaches row {owner_of_dismiss[0]} (doc_id {doc_id!r})"
        )

    @pytest.mark.parametrize("dismiss,dismissed,doc_id", _PAIRS)
    def test_the_live_matcher_runs_the_same_actions(
        self, matcher, dismiss, dismissed, doc_id
    ):
        expected = matcher.match_complete(dismiss, hotword_active=False)
        assert expected is not None and expected.matched
        result = matcher.match_complete(dismissed, hotword_active=False)
        assert result is not None and result.matched, (
            f"the shipped matcher does not fire on {dismissed!r}"
        )
        assert result.matched_text == dismissed
        assert result.remainder == "" and result.before_remainder == ""
        assert result.actions == expected.actions, (
            f"{dismissed!r} runs {result.actions!r}, but {dismiss!r} runs "
            f"{expected.actions!r}"
        )

    @pytest.mark.parametrize("dismiss,dismissed,doc_id", _PAIRS)
    def test_the_whole_utterance_executes(self, router, dismiss, dismissed, doc_id):
        from speech.domain import Action

        decision = router._resolve_finalization(
            dismissed.split(), hotword_active=False
        )
        assert decision.action is Action.EXECUTE, (
            f"{dismissed!r} spoken alone resolved to {decision.action}"
        )
        assert decision.payload == dismissed


class TestALongerSentenceStaysTypedText:
    @pytest.mark.parametrize("phrase", _SENTENCES)
    def test_the_sentence_dictates(self, router, phrase):
        from speech.domain import Action

        decision = router._resolve_finalization(phrase.split(), hotword_active=False)
        assert decision.action is Action.DICTATE, (
            f"{phrase!r} resolved to {decision.action}; the leading "
            f'"dismissed" ran a command and the rest of the sentence was lost'
        )
        assert decision.payload == phrase


class TestNoDismissRowLacksTheDismissedForm:
    def test_every_row_with_the_word_dismiss_also_accepts_dismissed(self, rows):
        # Acceptance item 4: a later row that takes the word "dismiss" must
        # take "dismissed" too. The word test is on the regex source, so a
        # function name such as grid_dismiss_command does not count.
        word = re.compile(r"(?<![A-Za-z_])dismiss(?![A-Za-z_])", re.IGNORECASE)
        missing = [
            row["pattern"]
            for row in rows
            if word.search(row["pattern"])
            and not re.search(r"(?<![A-Za-z_])dismissed(?![A-Za-z_])",
                              row["pattern"], re.IGNORECASE)
        ]
        assert missing == [], (
            f"these shipped rows accept 'dismiss' but not 'dismissed': {missing}"
        )
