"""Every spoken delete command also accepts the word erase.

Bead wh-erase-synonym-for-delete. David asked for "erase" as a synonym for
"delete" in every spoken command in the pattern files.

This test reads the pattern list from
services/wheelhouse/speech/config/patterns.toml at run time. It does not
carry its own copy of the wordings. A delete command added later without an
erase form therefore fails this test, and the failure message names the
remedy in plain words.

Measured on a32dbfe9: 18 pattern entries carry the word delete, and all 18
set whole_utterance_only = true.
"""

import re
import sys
import tomllib
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_PATTERNS_FILE = _SERVICE_ROOT / "speech" / "config" / "patterns.toml"

# The count measured on a32dbfe9, the base of this change. The check below
# exists so an empty selection cannot make the whole test pass silently.
_DELETE_PATTERN_COUNT_AT_BASE = 18

REMEDY = (
    "Remedy: add erase beside delete in the named pattern. Write the pattern "
    "as ^(?:delete|erase) followed by the rest of the wording, so one command "
    "accepts both words. Do not add a second pattern block, and do not change "
    "any doc_id."
)

# The only regex constructs the delete commands use. The expander below
# raises on anything else, so a new construct fails this test loudly rather
# than skipping the check for that pattern.
_KNOWN_GROUPS = {
    "(?:delete|erase)": "delete",
    "(?:this )?": "this ",
    r"(\d+)?": "3",
    r"\s*": " ",
    r"\s+": " ",
}


def _entries():
    """Every pattern entry in the shipped file, in file order."""
    with _PATTERNS_FILE.open("rb") as handle:
        return tomllib.load(handle)["pattern"]


def _delete_entries():
    """Every pattern entry whose regex contains the word delete."""
    return [e for e in _entries() if "delete" in e["pattern"].lower()]


def _spoken_form(regex):
    """Build one spoken phrase that the regex accepts."""
    if not (regex.startswith("^") and regex.endswith("$")):
        raise ValueError(f"pattern is not anchored at both ends: {regex!r}")
    body = regex[1:-1]
    spoken = []
    index = 0
    while index < len(body):
        for token, text in _KNOWN_GROUPS.items():
            if body.startswith(token, index):
                spoken.append(text)
                index += len(token)
                break
        else:
            char = body[index]
            if char in "()[]{}|*+\\?":
                raise ValueError(
                    f"unhandled regex construct {char!r} at index {index} "
                    f"in {regex!r}"
                )
            if index + 1 < len(body) and body[index + 1] == "?":
                # An optional letter, such as the s of characters?
                spoken.append(char)
                index += 2
            else:
                spoken.append(char)
                index += 1
    return re.sub(r"\s+", " ", "".join(spoken)).strip()


def _first_file_order_match(spoken_text):
    """The entry a spoken phrase resolves to under first-match-wins.

    This mirrors the walk test_va_delete_cut_copy_range.py uses, so both
    tests answer the ordering question the same way.
    """
    return next(
        (
            entry
            for entry in _entries()
            if re.compile(entry["pattern"], re.IGNORECASE).search(spoken_text)
        ),
        None,
    )


def test_the_pattern_file_still_carries_its_delete_commands():
    """Guard against an empty selection passing every other check."""
    found = len(_delete_entries())
    assert found >= _DELETE_PATTERN_COUNT_AT_BASE, (
        f"{_PATTERNS_FILE} holds {found} pattern entries containing delete. "
        f"At least {_DELETE_PATTERN_COUNT_AT_BASE} were present on a32dbfe9. "
        "If a delete command was removed on purpose, lower the number in "
        "_DELETE_PATTERN_COUNT_AT_BASE and say why in the commit message."
    )


@pytest.mark.parametrize(
    "entry", _delete_entries(), ids=lambda entry: entry["doc_id"]
)
def test_delete_command_also_accepts_erase(entry):
    """The erase wording reaches the same entry, with the same actions."""
    delete_phrase = _spoken_form(entry["pattern"])
    assert re.compile(entry["pattern"], re.IGNORECASE).match(delete_phrase), (
        "this test could not build a phrase its own pattern accepts. "
        f"doc_id {entry['doc_id']}, pattern {entry['pattern']!r}, "
        f"phrase {delete_phrase!r}. Fix _spoken_form, not the pattern."
    )

    erase_phrase = delete_phrase.replace("delete", "erase", 1)
    assert erase_phrase != delete_phrase, (
        f"the built phrase {delete_phrase!r} contains no word delete, so "
        f"this test cannot check doc_id {entry['doc_id']}."
    )

    matched = _first_file_order_match(erase_phrase)
    assert matched is not None, (
        f"the spoken command {erase_phrase!r} matches no pattern in "
        f"{_PATTERNS_FILE.name}. The delete wording {delete_phrase!r} works, "
        f"and doc_id {entry['doc_id']} is the pattern that accepts it. "
        f"{REMEDY}"
    )
    assert matched["doc_id"] == entry["doc_id"], (
        f"the spoken command {erase_phrase!r} reached doc_id "
        f"{matched['doc_id']!r}, but the delete wording {delete_phrase!r} "
        f"reaches doc_id {entry['doc_id']!r}. An earlier pattern in the file "
        f"takes the erase wording first. {REMEDY}"
    )
    assert matched["actions"] == entry["actions"], (
        f"the spoken command {erase_phrase!r} runs {matched['actions']!r}, "
        f"but {delete_phrase!r} runs {entry['actions']!r}. Both wordings must "
        f"run the same actions. {REMEDY}"
    )


@pytest.mark.parametrize(
    "entry", _delete_entries(), ids=lambda entry: entry["doc_id"]
)
def test_erase_is_a_recognised_first_word(entry):
    """The catalog must offer erase as a first word, or nothing matches.

    PatternCatalog indexes every pattern by its possible first words. A
    pattern whose first words omit erase is never even tried against a
    spoken phrase that starts with erase, however well its regex reads.
    """
    sys.path.insert(0, str(_SERVICE_ROOT))
    try:
        from speech.pattern_catalog import PatternCatalog
    finally:
        sys.path.pop(0)

    catalog = PatternCatalog.__new__(PatternCatalog)
    first_words = catalog._extract_first_words(entry["pattern"])
    assert "erase" in first_words, (
        f"PatternCatalog reads the first words of doc_id "
        f"{entry['doc_id']!r} as {first_words!r}, which omits erase. A "
        f"spoken phrase starting with erase never reaches this pattern. "
        f"{REMEDY}"
    )


@pytest.mark.parametrize(
    "entry", _delete_entries(), ids=lambda entry: entry["doc_id"]
)
def test_delete_command_stays_whole_utterance_only(entry):
    """Both wordings are ordinary English words, so both need the flag.

    Without whole_utterance_only the sentence "delete all the files" wiped
    the focused document at its second word. That is recorded in
    patterns.toml above the delete-all entry.
    """
    assert entry.get("whole_utterance_only") is True, (
        f"doc_id {entry['doc_id']!r} accepts delete and erase but does not "
        "set whole_utterance_only = true. Both are ordinary English words, "
        "so the command must fire only when the word is the whole utterance."
    )
