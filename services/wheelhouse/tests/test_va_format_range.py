"""Tests for wh-voice-access-parity.1.4 -- Voice Access format-over-a-range
commands.

STAGED FRAGMENT TEST. This file tests the PRODUCTION pattern catalog at
services/wheelhouse/speech/config/patterns.toml directly (the same idiom as
tests/test_pattern_matcher_gaps.py), because the fragment
patterns-staging/wh-voice-access-parity.1.4.toml is not wired into that file
yet -- placement is the boss's decision. Every test in this file is EXPECTED
TO FAIL until the 48 blocks in the sibling .toml fragment are merged into
patterns.toml. Do not "fix" that by adding the patterns here or by editing
patterns.toml from this file.

Mirrors the fragment's own data table (action verb, unit, direction) so a
future person merging both files can diff them side by side. See the
fragment header for the key-combo table, the requires_hotword decision, the
ordering-hazard check, and the count-vs-scope note.

IDENTIFYING A MATCH WITHOUT doc_id: ``doc_id`` is a TOML-only label. Verified
by grepping ``services/wheelhouse/speech/*.py`` for ``doc_id`` (no hits) and
by inspecting a matched ``MatchResult.pattern_data`` for the existing
``copy-line`` entry at runtime -- its keys are exactly ``['actions',
'literal_body_matchers', 'requires_hotword']``, with no ``doc_id`` anywhere.
``PatternCatalog`` never reads the key into the runtime pattern dict, for
this fragment's patterns or any pre-existing one, so a test that asserted
``result.pattern_data.get("doc_id") == doc_id`` would fail forever, not just
until the fragment is merged. This file identifies a match instead by its
resolved ``actions`` -- the (selection-extend keys, format action) pair is
unique across all 48 forms in the fragment, which this file's own
``TestActionPairsAreUnique`` proves, so asserting on ``actions`` is a
complete substitute for asserting on ``doc_id``.
"""
import sys
from pathlib import Path

# The bare `speech` imports below resolve with the service directory itself
# on sys.path.
service_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(service_dir))

import pytest

from speech.pattern_matcher import PatternMatcher
from speech.pattern_catalog import PatternCatalog


# ---------------------------------------------------------------------------
# Data table -- must match patterns-staging/wh-voice-access-parity.1.4.toml
# ---------------------------------------------------------------------------

# (unit_plural, unit_singular, forward_keys, backward_keys)
_UNITS = [
    ("words", "word", ["shift", "ctrl", "right"], ["shift", "ctrl", "left"]),
    ("lines", "line", ["shift", "down"], ["shift", "up"]),
    ("paragraphs", "paragraph", ["shift", "ctrl", "down"], ["shift", "ctrl", "up"]),
    ("characters", "character", ["shift", "right"], ["shift", "left"]),
]

# (doc_id_verb, spoken_verb, format_action)
# format_action is (function_name, params) for the SECOND action step.
_ACTIONS = [
    ("bold", "bold", ("hk", ["ctrl", "b"])),
    ("italicize", "italicize", ("hk", ["ctrl", "i"])),
    ("underline", "underline", ("hk", ["ctrl", "u"])),
    ("uppercase", "uppercase", ("transform_selection", ["uppercase"])),
    ("lowercase", "lowercase", ("transform_selection", ["lowercase"])),
    ("capitalize", "capitalize", ("transform_selection", ["capitalize"])),
]

# Alternate "previous" and "last" across units so both direction words in
# the (?:previous|last) alternation get exercised across the full sweep,
# without doubling every single test.
_BACKWARD_WORDS = ["previous", "last"]


def _all_forms():
    """Yield one row per SCOPE form: (doc_id, utterance, forward, keys, fmt)."""
    rows = []
    for verb_id, spoken_verb, fmt in _ACTIONS:
        for unit_index, (plural, singular, fwd_keys, bwd_keys) in enumerate(_UNITS):
            doc_id_next = f"{verb_id}-next-{plural}"
            utter_next = f"{spoken_verb} next three {plural}"
            rows.append((doc_id_next, utter_next, fwd_keys, fmt, "3-ish"))

            back_word = _BACKWARD_WORDS[unit_index % 2]
            doc_id_prev = f"{verb_id}-prev-{plural}"
            utter_prev = f"{spoken_verb} {back_word} two {plural}"
            rows.append((doc_id_prev, utter_prev, bwd_keys, fmt, "2-ish"))
    return rows


_FORMS = _all_forms()


@pytest.fixture(scope="module")
def matcher():
    catalog = PatternCatalog("speech/config/patterns.toml")
    return PatternMatcher(catalog)


# ---------------------------------------------------------------------------
# Every SCOPE form: correct doc_id, correct selection-extend keys, correct
# repeat slot, correct formatting step.
# ---------------------------------------------------------------------------

class TestEveryScopeFormMatchesAndActsCorrectly:
    @pytest.mark.parametrize(
        ("doc_id", "utterance", "extend_keys", "fmt", "_label"),
        _FORMS,
        ids=[row[0] for row in _FORMS],
    )
    def test_form(self, matcher, doc_id, utterance, extend_keys, fmt, _label):
        result = matcher.match_complete(utterance)

        assert result is not None and result.matched, (
            f"{utterance!r} must match the {doc_id!r} block once the "
            "fragment is merged into patterns.toml; currently expected to "
            "fail because the fragment is staged-only (see file docstring)."
        )
        assert result.pattern_data.get("requires_hotword", False) is False, (
            "requires_hotword must match the existing bold/italics/"
            "underline/uppercase/lowercase/capitalize entries (false)"
        )

        actions = result.actions
        assert len(actions) == 2, doc_id

        step1 = actions[0]
        assert step1["function"] == "hk"
        assert step1["params"] == [*extend_keys, "g1"], (
            "the real capture group's gN slot must be the LAST hk argument "
            f"(epic lexer rule); got {step1['params']!r}"
        )
        assert step1.get("awaits_done") is True, (
            "selection extension must complete before the formatting step "
            f"runs, doc_id={doc_id}"
        )

        step2 = actions[1]
        fmt_function, fmt_params = fmt
        assert step2["function"] == fmt_function
        assert step2["params"] == fmt_params


class TestActionPairsAreUnique:
    def test_all_48_action_pairs_are_distinct(self):
        # Justifies identifying a match by its actions instead of doc_id
        # (see module docstring): if two forms shared an action pair,
        # asserting on actions alone could not tell them apart.
        signatures = {
            (tuple(extend_keys), fmt[0], tuple(fmt[1]))
            for _doc_id, _utt, extend_keys, fmt, _l in _FORMS
        }
        assert len(signatures) == len(_FORMS) == 48


# ---------------------------------------------------------------------------
# Repeat count: digits, spoken number words, and the bare-singular
# (no number spoken -> N implied as 1) form all resolve through the same
# real capture group.
# ---------------------------------------------------------------------------

class TestRepeatCountCaptureGroup:
    def test_digit_count(self, matcher):
        result = matcher.match_complete("bold next 3 words")
        assert result is not None and result.matched
        assert result.actions[0]["params"] == ["shift", "ctrl", "right", "g1"]
        assert result.actions[1] == {"function": "hk", "params": ["ctrl", "b"]}
        assert result.group(1) == "3"
        assert result.validation_group == "g1"

    def test_spoken_number_word_count(self, matcher):
        # Proves the epic's lexer rule: a REAL capture group whose whole
        # body is \d+ gets widened so a spoken number word ("three") is
        # accepted and later resolved via words_to_int, per
        # speech/pattern_transform.py:_transform_numeric_captures and
        # speech/command_engine.py's validation_group check.
        result = matcher.match_complete("italicize previous two lines")
        assert result is not None and result.matched
        assert result.actions[0]["params"] == ["shift", "up", "g1"]
        assert result.actions[1] == {"function": "hk", "params": ["ctrl", "i"]}
        assert result.group(1) == "two"
        assert result.validation_group == "g1"

    def test_bare_singular_no_number_defaults_to_one(self, matcher):
        # The measured gap example from the dispatcher: no number is
        # spoken at all, N is implied as 1 by the singular noun. The
        # capture group must still be the REAL (\d+)? group (so
        # actions.hotkey()'s None-handling applies), not a separate
        # un-numbered pattern.
        result = matcher.match_complete("capitalize next word")
        assert result is not None and result.matched
        assert result.actions[0]["params"] == ["shift", "ctrl", "right", "g1"]
        assert result.actions[1] == {
            "function": "transform_selection",
            "params": ["capitalize"],
            "awaits_done": True,
        }
        assert result.group(1) is None

    def test_bare_singular_paragraph_backward(self, matcher):
        result = matcher.match_complete("underline previous paragraph")
        assert result is not None and result.matched
        assert result.actions[0]["params"] == ["shift", "ctrl", "up", "g1"]
        assert result.actions[1] == {"function": "hk", "params": ["ctrl", "u"]}
        assert result.group(1) is None


# ---------------------------------------------------------------------------
# "previous" and "last" are true synonyms sharing one backward pattern.
# ---------------------------------------------------------------------------

class TestPreviousAndLastAreSynonyms:
    _EXPECTED_ACTIONS = [
        {"function": "hk", "params": ["shift", "ctrl", "left", "g1"], "awaits_done": True},
        {"function": "transform_selection", "params": ["uppercase"], "awaits_done": True},
    ]

    def test_previous_wording(self, matcher):
        result = matcher.match_complete("uppercase previous four words")
        assert result is not None and result.matched
        assert result.actions == self._EXPECTED_ACTIONS

    def test_last_wording(self, matcher):
        # The dispatcher's measured gap example, verbatim. Must resolve
        # to the SAME actions as "previous" above -- proof they share one
        # backward pattern block rather than being two separate entries
        # that could drift apart.
        result = matcher.match_complete("uppercase last four words")
        assert result is not None and result.matched
        assert result.actions == self._EXPECTED_ACTIONS


# ---------------------------------------------------------------------------
# "upper case" / "lower case" two-word spellings (matches the existing
# ^upper ?case / ^lower ?case entries' own tolerance).
# ---------------------------------------------------------------------------

class TestUpperLowerCaseTwoWordSpelling:
    def test_upper_case_two_words(self, matcher):
        result = matcher.match_complete("upper case next five characters")
        assert result is not None and result.matched
        assert result.actions[0]["params"] == ["shift", "right", "g1"]
        assert result.actions[1]["params"] == ["uppercase"]

    def test_lower_case_two_words(self, matcher):
        result = matcher.match_complete("lower case last character")
        assert result is not None and result.matched
        assert result.actions[0]["params"] == ["shift", "left", "g1"]
        assert result.actions[1]["params"] == ["lowercase"]
        assert result.group(1) is None


# ---------------------------------------------------------------------------
# Exclusion: spoken-text forms ("bold hello") belong to a different bead
# and must not be produced by this fragment.
# ---------------------------------------------------------------------------

class TestSpokenTextFormsExcluded:
    @pytest.mark.parametrize(
        "utterance",
        [
            "bold hello",
            "italicize the quick brown fox",
            "underline hello world",
            "capitalize hello",
            "uppercase hello",
            "lowercase hello",
        ],
    )
    def test_spoken_text_form_not_produced_by_this_fragment(self, matcher, utterance):
        # These may or may not match some OTHER existing pattern (e.g. the
        # short "bold that" forms do not, but this is not this fragment's
        # concern). What this fragment guarantees is that none of ITS OWN
        # 48 forms (identified by their unique action-pair signature, see
        # TestActionPairsAreUnique) ever fires for a spoken-text form,
        # because none of its capture groups accept free text -- every
        # capture group is strictly (\d+)?.
        result = matcher.match_complete(utterance)
        range_signatures = {
            (tuple(extend_keys), fmt[0], tuple(fmt[1]))
            for _doc_id, _utt, extend_keys, fmt, _l in _FORMS
        }
        if result is not None and result.matched and len(result.actions) == 2:
            step1, step2 = result.actions
            if step1.get("function") == "hk":
                signature = (
                    tuple(step1.get("params", [])[:-1]),
                    step2.get("function"),
                    tuple(step2.get("params", [])),
                )
                assert signature not in range_signatures
