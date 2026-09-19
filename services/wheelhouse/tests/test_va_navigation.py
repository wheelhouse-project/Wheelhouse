"""Tests for wh-voice-access-parity.1.5 -- line and document navigation.

STAGING FILE: this lives in patterns-staging/, a scratch area outside
services/wheelhouse/tests/, alongside the pattern fragment it tests
(wh-voice-access-parity.1.5.toml). It is self-contained on purpose (no
`from speech...` imports) so it does not depend on services/wheelhouse being
on sys.path from this location.

Two groups of checks:

1. FRAGMENT checks (against wh-voice-access-parity.1.5.toml directly).
   These pass today: the fragment file already exists with the final
   pattern shapes. They are a standing sanity check on the fragment's
   regex/action shape, independent of where the boss ends up placing it.

2. LIVE-FILE checks (against the real
   services/wheelhouse/speech/config/patterns.toml). These CANNOT pass
   until the boss splices every block from the fragment into that file,
   ABOVE the existing cursor-navigate entry (see the fragment's header
   comment for why the placement is load bearing). Until that happens,
   every live-file assertion below fails with a "doc_id not found"
   message naming the missing doc_id -- that is the expected, correct
   pre-merge state. Do not edit patterns.toml to make these pass; the
   boss owns placement.

Also covered: the existing "go" and "grab" triggers must still match their
own entries once this fragment sits above them. There is exactly one
top-level doc_id for both: "cursor-navigate" (pattern ``^go (.+)``, section
"COMMANDS - Cursor Navigation"). grep -n "'''\\^grab" and
grep -n 'doc_id = "grab' over
services/wheelhouse/speech/config/patterns.toml both found no match -- there
is no separate top-level "grab" entry. "grab" only ever fires as the second
half of a "go X then grab Y" chain: NavigationParser.parse() (speech/
navigation/parser.py) splits the captured tail on " then " and accepts verb
"go" or "grab" per segment, but the FIRST segment of the whole utterance
must still start with "go" to satisfy patterns.toml's own ``^go (.+)``
anchor. A bare utterance that starts with "grab" (no leading "go ...") does
not match cursor-navigate or anything else in patterns.toml today
(grep -n "'''\\^grab" over patterns.toml: no match), so it falls through to
dictation -- that is pre-existing behavior, not something this bead touches
or changes.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[2]
_FRAGMENT_PATH = _REPO_ROOT / "patterns-staging" / "wh-voice-access-parity.1.5.toml"
_LIVE_PATTERNS_PATH = (
    _REPO_ROOT / "services" / "wheelhouse" / "speech" / "config" / "patterns.toml"
)


def _require_fragment() -> Path:
    if not _FRAGMENT_PATH.is_file():
        pytest.skip("development-only patterns-staging input is absent from this checkout")
    return _FRAGMENT_PATH


def _load_pattern_blocks(path: Path) -> list[dict]:
    if path == _FRAGMENT_PATH:
        _require_fragment()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return data["pattern"]


def _index_by_doc_id(blocks: list[dict]) -> dict[str, dict]:
    return {b["doc_id"]: b for b in blocks if "doc_id" in b}


def _index_position(blocks: list[dict]) -> dict[str, int]:
    return {b["doc_id"]: i for i, b in enumerate(blocks) if "doc_id" in b}


def _require_doc_id(by_id: dict[str, dict], doc_id: str, source: Path) -> dict:
    assert doc_id in by_id, (
        f"doc_id {doc_id!r} not found in {source}. If source is the live "
        "patterns.toml, this is expected until the boss splices "
        "patterns-staging/wh-voice-access-parity.1.5.toml above the "
        "cursor-navigate entry -- see that fragment's header comment."
    )
    return by_id[doc_id]


def _compile(block: dict) -> re.Pattern:
    return re.compile(block["pattern"], re.IGNORECASE)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fragment_blocks() -> list[dict]:
    return _load_pattern_blocks(_FRAGMENT_PATH)


@pytest.fixture(scope="module")
def fragment_by_id(fragment_blocks) -> dict[str, dict]:
    return _index_by_doc_id(fragment_blocks)


@pytest.fixture(scope="module")
def live_blocks() -> list[dict]:
    return _load_pattern_blocks(_LIVE_PATTERNS_PATH)


@pytest.fixture(scope="module")
def live_by_id(live_blocks) -> dict[str, dict]:
    return _index_by_doc_id(live_blocks)


@pytest.fixture(scope="module")
def live_position(live_blocks) -> dict[str, int]:
    return _index_position(live_blocks)


# ---------------------------------------------------------------------------
# Expected shape table: one row per SCOPE form family (21 pattern blocks,
# covering 29 spoken forms -- 8 blocks cover both a "go" and a "move"
# wording via a non-capturing (?:go|move) alternation).
#
# Each row: doc_id, expected actions, and sample (utterance, expected g1)
# pairs. g1 is None where the block takes no repeat count. Samples use
# digit counts ("3", "12") only -- this file compiles each pattern's raw
# regex with the stdlib re module, and a raw (\d+) group does not match a
# spoken number word like "three". Word-number capture is a separate
# widening pass, _transform_numeric_captures in speech/pattern_transform.py,
# applied by PatternCatalog at load time, not by re.compile on the bare TOML
# string. That widening is exercised by TestNumericTransformSyntax in
# test_pattern_transform.py at the syntax level, and end to end by
# test_speech_processor_bare_number.py, which drives spoken number words
# through to an executed "click twenty three". It used to be exercised by
# tests/speech/test_text_parser.py and tests/speech/test_speech_router.py too
# (wh-voice-access-parity epic comment, 2026-08-17); that repo-root suite was
# retired under wh-speech-suite-repair-or-retire. It is not re-tested here;
# this file only proves each block's
# capture group is real and positioned correctly (see
# test_fragment_counted_blocks_use_real_capture_group_feeding_g1 below),
# which is what makes that widening apply in the first place.
# ---------------------------------------------------------------------------

_HK = "hk"

EXPECTED = [
    # -- vertical: lines --
    (
        "nav-up-lines",
        [{"function": _HK, "params": ["up", "g1"]}],
        [
            ("go up 3 lines", "3"),
            ("move up 3 lines", "3"),
            ("go up line", None),
            ("move up lines", None),
        ],
    ),
    (
        "nav-down-lines",
        [{"function": _HK, "params": ["down", "g1"]}],
        [
            ("go down 12 lines", "12"),
            ("move down line", None),
        ],
    ),
    (
        "nav-up-paragraphs",
        [{"function": _HK, "params": ["ctrl", "up", "g1"]}],
        [
            ("go up 2 paragraphs", "2"),
            ("move up paragraph", None),
        ],
    ),
    (
        "nav-down-paragraphs",
        [{"function": _HK, "params": ["ctrl", "down", "g1"]}],
        [
            ("go down 5 paragraphs", "5"),
            ("move down paragraph", None),
        ],
    ),
    # -- horizontal: characters --
    (
        "nav-left-characters",
        [{"function": _HK, "params": ["left", "g1"]}],
        [
            ("go left 4 characters", "4"),
            ("move left character", None),
        ],
    ),
    (
        "nav-right-characters",
        [{"function": _HK, "params": ["right", "g1"]}],
        [
            ("go right 7 characters", "7"),
            ("move right character", None),
        ],
    ),
    # -- horizontal: words --
    (
        "nav-left-words",
        [{"function": _HK, "params": ["ctrl", "left", "g1"]}],
        [
            ("go left 2 words", "2"),
            ("move left word", None),
        ],
    ),
    (
        "nav-right-words",
        [{"function": _HK, "params": ["ctrl", "right", "g1"]}],
        [
            ("go right 6 words", "6"),
            ("move right word", None),
        ],
    ),
    # -- bare landmarks (already reachable via cursor-navigate today) --
    (
        "nav-go-top",
        [{"function": _HK, "params": ["ctrl", "home"]}],
        [("go to top", None)],
    ),
    (
        "nav-go-bottom",
        [{"function": _HK, "params": ["ctrl", "end"]}],
        [("go to bottom", None)],
    ),
    (
        "nav-go-end",
        [{"function": _HK, "params": ["end"]}],
        [("go to end", None)],
    ),
    # -- of-document / of-line / of-word / of-paragraph --
    (
        "nav-go-beginning-of-document",
        [{"function": _HK, "params": ["ctrl", "home"]}],
        [
            ("go to the beginning of the document", None),
            ("go to beginning of document", None),
        ],
    ),
    (
        "nav-go-end-of-document",
        [{"function": _HK, "params": ["ctrl", "end"]}],
        [("go to the end of the document", None)],
    ),
    (
        "nav-go-beginning-of-line",
        [{"function": _HK, "params": ["home"]}],
        [("go to the beginning of the line", None)],
    ),
    (
        "nav-go-end-of-line",
        [{"function": _HK, "params": ["end"]}],
        [("go to the end of the line", None)],
    ),
    (
        "nav-go-beginning-of-word",
        [{"function": _HK, "params": ["ctrl", "left"]}],
        [("go to the beginning of the word", None)],
    ),
    (
        "nav-go-end-of-word",
        [{"function": _HK, "params": ["ctrl", "right"]}],
        [("go to the end of the word", None)],
    ),
    (
        "nav-go-beginning-of-paragraph",
        [{"function": _HK, "params": ["ctrl", "up"]}],
        [("go to the beginning of the paragraph", None)],
    ),
    (
        "nav-go-end-of-paragraph",
        [{"function": _HK, "params": ["ctrl", "down"]}],
        [("go to the end of the paragraph", None)],
    ),
    # -- selection --
    (
        "nav-move-beginning-of-selection",
        [{"function": _HK, "params": ["left"]}],
        [("move to the beginning of the selection", None)],
    ),
    (
        "nav-move-end-of-selection",
        [{"function": _HK, "params": ["right"]}],
        [("move to the end of the selection", None)],
    ),
]

assert len(EXPECTED) == 21, "expected 21 pattern blocks for this bead's SCOPE"


# ---------------------------------------------------------------------------
# 1. Fragment checks -- pass today.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc_id,expected_actions,samples", EXPECTED, ids=[e[0] for e in EXPECTED])
def test_fragment_block_matches_and_acts(doc_id, expected_actions, samples, fragment_by_id):
    block = _require_doc_id(fragment_by_id, doc_id, _FRAGMENT_PATH)
    assert block["actions"] == expected_actions, (
        f"{doc_id}: fragment actions {block['actions']!r} != expected {expected_actions!r}"
    )
    rx = _compile(block)
    for utterance, expected_g1 in samples:
        m = rx.match(utterance)
        assert m is not None, f"{doc_id}: pattern {block['pattern']!r} did not match {utterance!r}"
        if expected_actions[-1]["params"][-1] == "g1":
            assert m.group(1) == expected_g1, (
                f"{doc_id}: {utterance!r} captured g1={m.group(1)!r}, expected {expected_g1!r}"
            )


def test_fragment_counted_blocks_use_real_capture_group_feeding_g1(fragment_by_id):
    """Lexer rule (wh-voice-access-parity epic, 2026-08-17): a repeat count
    must come from a real capturing group whose entire body is \\d+, and its
    real gN slot must be the last argument to hk. A bare \\d+ or a
    non-capturing (?:\\d+) does not get spoken-number-word support.
    """
    counted_doc_ids = [
        doc_id
        for doc_id, actions, _samples in EXPECTED
        if actions[-1]["params"][-1] == "g1"
    ]
    assert len(counted_doc_ids) == 8, "expected 8 counted movement blocks"
    for doc_id in counted_doc_ids:
        block = fragment_by_id[doc_id]
        pattern = block["pattern"]
        # Exactly one real capturing group, and its source is exactly \d+
        # (written optional as (\d+)? in every block here).
        assert "(\\d+)" in pattern, f"{doc_id}: no literal (\\d+) capture group in {pattern!r}"
        assert "(?:\\d+)" not in pattern, f"{doc_id}: non-capturing (?:\\d+) is not spoken-word aware"
        compiled = re.compile(pattern)
        assert compiled.groups == 1, (
            f"{doc_id}: expected exactly 1 capturing group, found {compiled.groups} in {pattern!r}"
        )
        assert block["actions"][-1]["params"][-1] == "g1", (
            f"{doc_id}: repeat count must be the last hk argument, got {block['actions'][-1]['params']!r}"
        )


def test_fragment_uncounted_blocks_have_no_capture_group(fragment_by_id):
    uncounted_doc_ids = [
        doc_id
        for doc_id, actions, _samples in EXPECTED
        if actions[-1]["params"][-1] != "g1"
    ]
    assert len(uncounted_doc_ids) == 13
    for doc_id in uncounted_doc_ids:
        block = fragment_by_id[doc_id]
        compiled = re.compile(block["pattern"])
        assert compiled.groups == 0, (
            f"{doc_id}: landmark/selection block should take no capture group, "
            f"found {compiled.groups} in {block['pattern']!r}"
        )


def test_fragment_header_states_placement_above_cursor_navigate():
    text = _require_fragment().read_text(encoding="utf-8")
    # Split on the first *real* [[pattern]] table marker (start of line),
    # not any mention of the literal text "[[pattern]]" inside a comment --
    # the header itself explains TOML table-array syntax and says
    # "[[pattern]] block" in prose, which a naive substring split would cut
    # into instead of treating as part of the header.
    match = re.search(r"(?m)^\[\[pattern\]\]", text)
    assert match is not None, "fragment has no [[pattern]] table at all"
    header = text[: match.start()]
    assert "ABOVE" in header and "cursor-navigate" in header, (
        "fragment header must state that every entry goes above the "
        "cursor-navigate entry (acceptance criteria for "
        "wh-voice-access-parity.1.5)"
    )


def test_move_forms_first_match_their_own_entries_in_live_file(live_blocks):
    """Post-placement truth (fragment spliced 2026-08-24): every "move ..."
    navigation wording now first-matches its own nav-* entry in the live
    file. The pre-placement version of this test asserted the opposite --
    that no live block matched these forms, the duplicate check that
    cleared the fragment for splicing."""
    expected = [
        ("move up 3 lines", "nav-up-lines"),
        ("move down 2 paragraphs", "nav-down-paragraphs"),
        ("move left 4 characters", "nav-left-characters"),
        ("move right 6 words", "nav-right-words"),
        ("move to the beginning of the selection",
         "nav-move-beginning-of-selection"),
        ("move to the end of the selection", "nav-move-end-of-selection"),
    ]
    for utterance, expected_doc_id in expected:
        first = next(
            (b for b in live_blocks if _compile(b).search(utterance)), None
        )
        assert first is not None, (
            f"{utterance!r} matches nothing in the live file"
        )
        assert first.get("doc_id") == expected_doc_id, (
            f"{utterance!r} first matched {first.get('doc_id')!r}, not "
            f"{expected_doc_id!r} -- an earlier block swallows it "
            "(first match wins, patterns.toml ORDERING RULES)"
        )


# ---------------------------------------------------------------------------
# 2. Live-file checks -- cannot pass until the boss places this fragment's
#    blocks above cursor-navigate in services/wheelhouse/speech/config/
#    patterns.toml. Expected to fail with a clear "doc_id not found"
#    message until then.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc_id,expected_actions,samples", EXPECTED, ids=[e[0] for e in EXPECTED])
def test_live_patterns_toml_block_matches_and_acts(doc_id, expected_actions, samples, live_by_id):
    block = _require_doc_id(live_by_id, doc_id, _LIVE_PATTERNS_PATH)
    assert block["actions"] == expected_actions
    rx = _compile(block)
    for utterance, expected_g1 in samples:
        m = rx.match(utterance)
        assert m is not None, f"{doc_id}: pattern {block['pattern']!r} did not match {utterance!r}"
        if expected_actions[-1]["params"][-1] == "g1":
            assert m.group(1) == expected_g1


def test_live_patterns_toml_places_every_scope_form_above_cursor_navigate(live_position):
    cursor_navigate_pos = live_position.get("cursor-navigate")
    assert cursor_navigate_pos is not None, (
        "doc_id 'cursor-navigate' not found in the live patterns.toml -- "
        "has it been renamed or removed? This bead must not touch it."
    )
    for doc_id, _actions, _samples in EXPECTED:
        pos = live_position.get(doc_id)
        assert pos is not None, (
            f"doc_id {doc_id!r} not found in the live patterns.toml yet "
            "(expected pre-merge)."
        )
        assert pos < cursor_navigate_pos, (
            f"{doc_id!r} is at position {pos}, cursor-navigate is at "
            f"{cursor_navigate_pos} -- {doc_id!r} must come BEFORE "
            "cursor-navigate or cursor-navigate's ^go (.+) pattern will "
            "swallow it first (first match wins, patterns.toml ORDERING "
            "RULES)."
        )


# ---------------------------------------------------------------------------
# Existing "go" and "grab" triggers must still match their own entries.
# Both are served by the single doc_id "cursor-navigate" (pattern
# ^go (.+)) -- see module docstring for the grep evidence that no separate
# top-level "grab" entry exists.
# ---------------------------------------------------------------------------


def test_existing_cursor_navigate_go_trigger_still_matches(live_by_id):
    block = _require_doc_id(live_by_id, "cursor-navigate", _LIVE_PATTERNS_PATH)
    rx = _compile(block)
    for utterance, expected_g1 in [
        ("go home", "home"),
        ("go end", "end"),
        ("go left two words", "left two words"),
        ("go start of word", "start of word"),
    ]:
        m = rx.match(utterance)
        assert m is not None, f"cursor-navigate no longer matches {utterance!r}"
        assert m.group(1) == expected_g1


def test_existing_cursor_navigate_grab_chain_still_matches(live_by_id):
    """"grab" has no entry of its own; it only ever fires as the tail of a
    "go X then grab Y" utterance, still routed through the single
    cursor-navigate doc_id (^go (.+) captures the whole tail, and
    NavigationParser.parse splits on " then " and accepts "grab" as a
    per-segment verb -- speech/navigation/parser.py _parse_segment)."""
    block = _require_doc_id(live_by_id, "cursor-navigate", _LIVE_PATTERNS_PATH)
    rx = _compile(block)
    for utterance, expected_g1 in [
        ("go home then grab to end", "home then grab to end"),
        ("go start of word then grab right two words", "start of word then grab right two words"),
    ]:
        m = rx.match(utterance)
        assert m is not None, f"cursor-navigate no longer matches grab chain {utterance!r}"
        assert m.group(1) == expected_g1


def test_bare_grab_utterance_still_matches_nothing_in_live_patterns(live_blocks):
    """Pre-existing behavior, unrelated to this bead: a bare "grab ..."
    utterance with no leading "go " does not match cursor-navigate (anchored
    on ^go) or any other live pattern, so it falls through to dictation.
    grep -n "'''\\^grab" and grep -n 'doc_id = "grab' over
    services/wheelhouse/speech/config/patterns.toml both found no match --
    there is no separate top-level grab trigger to regress."""
    for block in live_blocks:
        rx = _compile(block)
        assert not rx.match("grab to end"), (
            f"'grab to end' unexpectedly matches doc_id {block.get('doc_id')!r} "
            "-- this was not expected to match anything before this bead"
        )
