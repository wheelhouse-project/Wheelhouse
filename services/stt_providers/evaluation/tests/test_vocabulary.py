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


def test_loose_match_categories_only_differ_cosmetically(vocabulary):
    for u in vocabulary:
        if u.category not in LOOSE_MATCH_CATEGORIES:
            continue
        assert _cosmetic_only_diff(u.text, u.expected_transcription), (
            f"{u.category} entry text and expected differ in more than "
            f"sentence-start case or trailing punctuation: "
            f"text={u.text!r} expected={u.expected_transcription!r}"
        )
