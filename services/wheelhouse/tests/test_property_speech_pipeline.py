"""Property-based tests for the pure speech-pipeline logic (wh-bnu).

Voice input is inherently unpredictable, so these tests fuzz the pure
transformation layers with generated input instead of hand-picked
examples: the grapheme segmenter (shared/grapheme.py), TextPerfector,
SelectionTransformer, and PatternMatcher over the real pattern catalog.

Applied surgically per the bead: only pure functions, no hardware- or
UI-touching code. The SpeechProcessor truth table is NOT here -- it is
a stateful controller-bound machine, not a pure function, and its
behavior is covered by the scenario tests in
test_speech_processor_*.py.

The hypothesis profile keeps runs bounded: 60 examples per property,
no per-example deadline (CI machines vary widely; pytest-timeout's
30s per-test cap is the real guard).
"""
import string
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

# Add parent directories to path for imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.grapheme import (
    count_grapheme_clusters,
    normalize_line_endings,
    split_at_cluster_boundary_from_right,
)
from ui.selection_transformer import SelectionTransformer
from ui.text_perfector import TextPerfector

settings.register_profile(
    "wheelhouse",
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("wheelhouse")


# ---------------------------------------------------------------------------
# Grapheme segmenter invariants
# ---------------------------------------------------------------------------

# Code points chosen to stress every rule the hand-rolled segmenter
# implements: ZWJ/ZWNJ, variation selectors, tag characters, skin-tone
# selectors, combining marks, regional-indicator pairs, CR/LF, and
# non-BMP emoji -- mixed with plain ASCII.
_STRESS_ALPHABET = st.sampled_from([
    "a", "B", "3", " ", "\t", "\r", "\n",
    "́",        # combining acute accent (Mn)
    "‍",        # Zero Width Joiner
    "‌",        # Zero Width Non-Joiner
    "️",        # variation selector 16
    "\U0001F600",    # emoji (non-BMP)
    "\U0001F3FB",    # Fitzpatrick skin-tone selector
    "\U0001F1E6",    # regional indicator A
    "\U0001F1EB",    # regional indicator F
    "\U000E0062",    # tag character
    "中",        # CJK
])

_stress_text = st.text(alphabet=_STRESS_ALPHABET, max_size=40)


@given(text=_stress_text, n=st.integers(min_value=0, max_value=50))
def test_split_concatenation_identity(text, n):
    """kept + removed must reproduce the input byte-for-byte."""
    kept, removed = split_at_cluster_boundary_from_right(text, n)
    assert kept + removed == text


@given(text=_stress_text, n=st.integers(min_value=0, max_value=50))
def test_split_removed_cluster_count(text, n):
    """removed holds exactly min(n, total) clusters; kept the rest."""
    total = count_grapheme_clusters(text)
    kept, removed = split_at_cluster_boundary_from_right(text, n)
    assert count_grapheme_clusters(removed) == min(n, total)
    assert count_grapheme_clusters(kept) == total - min(n, total)


@given(text=_stress_text)
def test_split_edges(text):
    """n=0 removes nothing; n=total empties the kept side."""
    total = count_grapheme_clusters(text)
    assert split_at_cluster_boundary_from_right(text, 0) == (text, "")
    kept, removed = split_at_cluster_boundary_from_right(text, total or 1)
    assert kept == ""
    assert removed == text


@given(text=_stress_text)
def test_negative_split_raises(text):
    with pytest.raises(ValueError):
        split_at_cluster_boundary_from_right(text, -1)


@given(text=_stress_text)
def test_cluster_count_bounds(text):
    """Empty iff zero clusters; clusters never exceed code points."""
    total = count_grapheme_clusters(text)
    assert (total == 0) == (text == "")
    assert total <= len(text)


@given(text=_stress_text)
def test_normalize_line_endings_idempotent_and_cr_free(text):
    normalized = normalize_line_endings(text)
    assert "\r" not in normalized
    assert normalize_line_endings(normalized) == normalized


# ---------------------------------------------------------------------------
# TextPerfector invariants
# ---------------------------------------------------------------------------

_plain_words = st.text(
    alphabet=st.sampled_from(string.ascii_lowercase + " "), min_size=1, max_size=30
)
_printable = st.text(
    alphabet=st.sampled_from(string.printable), max_size=30
)


@given(insertion=_printable, preceding=_printable, sel=st.booleans(),
       cap=st.booleans())
def test_perfector_total_on_printable_input(insertion, preceding, sel, cap):
    """perfected_string never raises and always returns a string."""
    result = TextPerfector().perfected_string(
        insertion, preceding_chars=preceding, has_selection=sel, capitalize=cap
    )
    assert isinstance(result, str)


@given(insertion=_plain_words, preceding=_plain_words)
def test_perfector_preserves_letters(insertion, preceding):
    """For plain alphabetic dictation, perfection may adjust spacing and
    case but must never invent or drop letters."""
    result = TextPerfector().perfected_string(
        insertion, preceding_chars=preceding
    )
    assert result.replace(" ", "").lower() == insertion.replace(" ", "").lower()


# ---------------------------------------------------------------------------
# SelectionTransformer invariants
# ---------------------------------------------------------------------------

_WRAPS = {
    "quote": ('"', '"'),
    "single_quote": ("'", "'"),
    "bracket": ("[", "]"),
    "parenthesis": ("(", ")"),
}


@given(text=_printable, wrap=st.sampled_from(sorted(_WRAPS)))
def test_wrap_transformations_delimit_exactly(text, wrap):
    opener, closer = _WRAPS[wrap]
    result = SelectionTransformer().apply_transformation(text, wrap)
    assert result == f"{opener}{text}{closer}"


@given(text=_printable)
def test_case_transformations_match_str_methods(text):
    transformer = SelectionTransformer()
    assert transformer.apply_transformation(text, "uppercase") == text.upper()
    assert transformer.apply_transformation(text, "lowercase") == text.lower()


@given(text=st.text(
    alphabet=st.sampled_from(string.ascii_letters + " "), min_size=1, max_size=30
))
def test_programmer_cases_contain_no_spaces(text):
    transformer = SelectionTransformer()
    for kind in ("snake_case", "camel_case", "pascal_case", "kebab_case"):
        result = transformer.apply_transformation(text.strip() or "x", kind)
        assert result is not None
        assert " " not in result


@given(text=_printable)
def test_unknown_transformation_returns_none(text):
    assert SelectionTransformer().apply_transformation(text, "no_such_kind") is None


# ---------------------------------------------------------------------------
# PatternMatcher over the real catalog: total and deterministic
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def matcher():
    from speech.pattern_catalog import PatternCatalog
    from speech.pattern_matcher import PatternMatcher

    # user_patterns_file="" keeps the developer's personal user file out of
    # the property-test catalog (wh-user-patterns-split.12.1); the
    # session-scoped autouse guard in conftest.py is the first line of
    # defense, this explicit argument the second.
    return PatternMatcher(
        PatternCatalog("speech/config/patterns.toml", user_patterns_file="")
    )


# Mix of real command vocabulary and arbitrary word salad, so some
# buffers match real patterns and most do not.
_word = st.one_of(
    st.sampled_from([
        "select", "all", "undo", "new", "line", "backspace", "delete",
        "word", "go", "home", "end", "then", "period", "comma", "x-ray",
    ]),
    st.text(alphabet=st.sampled_from(string.ascii_lowercase), min_size=1,
            max_size=8),
)
_buffers = st.lists(_word, min_size=1, max_size=6)


def _result_shape(result):
    if result is None:
        return None
    return (result.matched, result.is_greedy)


@given(buffer=_buffers, pattern_type=st.sampled_from(["command", "replacement"]),
       hotword=st.booleans())
@settings(max_examples=60, deadline=None)
def test_matcher_total_and_deterministic(matcher, buffer, pattern_type, hotword):
    """Arbitrary word buffers never crash the matcher, and matching the
    same buffer twice gives the same decision (no hidden state)."""
    first = matcher.match_for_routing(buffer, pattern_type, hotword)
    second = matcher.match_for_routing(buffer, pattern_type, hotword)
    assert _result_shape(first) == _result_shape(second)
    complete = matcher.is_pattern_complete(buffer, pattern_type, hotword)
    assert isinstance(complete, bool)
