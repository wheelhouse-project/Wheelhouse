"""Per-category invariants for the TTS corpus vocabulary.

Catches drift between an Utterance's spoken text and its
expected_transcription when one is edited without the other. The rules
encoded here are the same conventions vocabulary.py is written to:

- single_word, multi_word, parameterized, punctuation, litmus: the
  spoken text and the expected canonical are the same string. There is
  no transformation between them.
- dictation, discontinuous: the canonical is the spoken text with
  cosmetic changes only -- sentence-start lowercasing and at most one
  trailing terminal punctuation character. The two strings must satisfy
  the harness's loose_match() comparator.
- itn: text and expected are independent canonical/spoken pairs and
  often differ in word-vs-digit form. The only structural rule is that
  both fields are non-empty.

Adding a category here requires adding it to one of the three groups
below, otherwise test_every_category_is_classified() fails.
"""

import re
import tomllib
from pathlib import Path

import pytest

from vocabulary import build_vocabulary

TERMINAL_PUNCT = ".?!"


def _strip_one_terminal_punct(text: str) -> str:
    if text and text[-1] in TERMINAL_PUNCT:
        return text[:-1].rstrip()
    return text


def _cosmetic_only_diff(text: str, expected: str) -> bool:
    """True if text and expected differ only in first-char case and a trailing terminal punctuation char.

    The vocabulary convention lowercases the sentence-start word in
    expected_transcription (unless the first word is itself a proper
    noun) and otherwise leaves the spoken text as-is. A trailing period,
    question mark, or exclamation point is also treated as cosmetic.
    Mid-string casing must match exactly.
    """
    a = _strip_one_terminal_punct(text)
    b = _strip_one_terminal_punct(expected)
    if not a or not b:
        return a == b
    if a[1:] != b[1:]:
        return False
    return a[0].lower() == b[0].lower()

VERBATIM_CATEGORIES = {
    "single_word",
    "multi_word",
    "parameterized",
    "punctuation",
    "litmus",
    # Voice Access parity command forms (wh-voice-access-parity.1.13), one
    # category per source bead. These are spoken commands, so they follow the
    # same convention as the other command categories above: the spoken text
    # and the expected canonical are the same string, with no transformation
    # between them. Measured at the time of writing: all 272 entries satisfy
    # text == expected_transcription.
    "va_punctuation_symbols",
    "va_select_range",
    "va_delete_cut_copy_range",
    "va_format_range",
    "va_navigation",
    "va_command_aliases",
    "va_window_app",
    "va_search",
    "va_literal_bypass",
}

LOOSE_MATCH_CATEGORIES = {
    "dictation",
    "discontinuous",
}

FREEFORM_CATEGORIES = {
    "itn",
}

KNOWN_CATEGORIES = (
    VERBATIM_CATEGORIES | LOOSE_MATCH_CATEGORIES | FREEFORM_CATEGORIES
)


@pytest.fixture(scope="module")
def vocabulary():
    return build_vocabulary()


def test_every_utterance_has_text_and_expected(vocabulary):
    for u in vocabulary:
        assert u.text, f"empty text in {u.category} entry"
        assert u.expected_transcription, (
            f"empty expected_transcription for text={u.text!r}"
        )


def test_every_category_is_classified(vocabulary):
    seen = {u.category for u in vocabulary}
    unknown = seen - KNOWN_CATEGORIES
    assert not unknown, (
        f"vocabulary uses categories {sorted(unknown)} that this test "
        f"does not classify; add them to VERBATIM_CATEGORIES, "
        f"LOOSE_MATCH_CATEGORIES, or FREEFORM_CATEGORIES"
    )


def test_verbatim_categories_have_identical_text_and_expected(vocabulary):
    for u in vocabulary:
        if u.category not in VERBATIM_CATEGORIES:
            continue
        assert u.text == u.expected_transcription, (
            f"{u.category} entry text and expected diverge: "
            f"text={u.text!r} expected={u.expected_transcription!r}"
        )


# "delete" is deliberately present twice: once as an ordinary single_word
# entry and once as the litmus probe (review finding
# wh-voice-access-parity.3.4 excluded it by design).
DELIBERATE_DUPLICATES = {"delete"}


def test_no_utterance_text_is_duplicated_across_categories(vocabulary):
    """build_vocabulary's single-representation rule (the .1.6 comment).

    A duplicated text is synthesized once per voice per category, so the
    same audio is scored under two categories and double-counts the
    per-category aggregates (review finding wh-voice-access-parity.3.4).
    """
    seen: dict[str, str] = {}
    duplicates = []
    for u in vocabulary:
        if u.text in DELIBERATE_DUPLICATES:
            continue
        if u.text in seen:
            duplicates.append(
                f"{u.text!r} in both {seen[u.text]} and {u.category}"
            )
        else:
            seen[u.text] = u.category
    assert not duplicates, "; ".join(duplicates)


def test_loose_match_categories_only_differ_cosmetically(vocabulary):
    for u in vocabulary:
        if u.category not in LOOSE_MATCH_CATEGORIES:
            continue
        assert _cosmetic_only_diff(u.text, u.expected_transcription), (
            f"{u.category} entry text and expected differ in more than "
            f"sentence-start case or trailing punctuation: "
            f"text={u.text!r} expected={u.expected_transcription!r}"
        )
# ---------------------------------------------------------------------------
# Finding wh-voice-access-parity.3.7 (Codex, round 2). The corpus said
# "search Google for X" while the live command is "search on google for X".
# Those rows therefore measured the catch-all '^search(?: for)? (.+)$',
# which captures "Google for X" as the search term, instead of the provider
# entry they were written to exercise. The two tests below keep the corpus
# and patterns.toml synchronized in both directions, so the same drift
# cannot return silently.
#
# "search Windows for X" is NOT affected and must keep its wording: the
# live entry is '^search windows for (.+)$', with no "on".
# ---------------------------------------------------------------------------

_PATTERNS_TOML = (
    Path(__file__).resolve().parents[4]
    / "services" / "wheelhouse" / "speech" / "config" / "patterns.toml"
)

_SEARCH_ON_ENTRY = re.compile(r"^\^search on (\w+) for \(\.\+\)\$$")


def _live_search_providers() -> set[str]:
    """Provider names taken from the live '^search on X for (.+)$' entries."""
    with _PATTERNS_TOML.open("rb") as handle:
        blocks = tomllib.load(handle).get("pattern", [])
    found = set()
    for block in blocks:
        match = _SEARCH_ON_ENTRY.match(block.get("pattern", ""))
        if match is not None:
            found.add(match.group(1).lower())
    return found


def _va_search_texts() -> list[str]:
    return [
        u.text.lower() for u in build_vocabulary() if u.category == "va_search"
    ]


class TestSearchProviderFormsMatchThePatternsFile:
    def test_the_live_file_still_has_provider_entries(self):
        """Guards the two tests below: an empty set makes them vacuous."""
        providers = _live_search_providers()
        assert {"google", "bing", "youtube"} <= providers, (
            "patterns.toml no longer declares the three provider search "
            f"entries; found {sorted(providers)!r}. Either the entries were "
            "renamed or this test's pattern shape is stale."
        )

    def test_the_corpus_speaks_the_on_form_for_every_provider(self):
        texts = _va_search_texts()
        missing = [
            provider
            for provider in sorted(_live_search_providers())
            if not any(
                text.startswith(f"search on {provider} for ") for text in texts
            )
        ]
        assert not missing, (
            f"patterns.toml has a 'search on X for' entry for {missing!r} but "
            "no va_search corpus utterance speaks that wording, so the "
            "benchmark never exercises the entry"
        )

    def test_the_corpus_never_speaks_the_on_less_provider_form(self):
        texts = _va_search_texts()
        offenders = sorted(
            {
                text
                for provider in _live_search_providers()
                for text in texts
                if text.startswith(f"search {provider} for ")
            }
        )
        assert not offenders, (
            "these va_search utterances drop the 'on' that the live entry "
            "requires, so they fall into the catch-all "
            f"'^search(?: for)? (.+)$' instead: {offenders!r}"
        )
