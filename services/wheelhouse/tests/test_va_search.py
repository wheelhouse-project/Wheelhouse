"""Tests for wh-voice-access-parity.1.8 -- Voice Access search commands.

STAGING FILE: this lives in patterns-staging/, a scratch area outside
services/wheelhouse/tests/, alongside the pattern fragment it tests
(wh-voice-access-parity.1.8.toml). It is self-contained on purpose (no
`from speech...` imports) so it does not depend on services/wheelhouse being
on sys.path from this location.

Three groups of checks:

1. FRAGMENT checks (against wh-voice-access-parity.1.8.toml directly).
   These pass today: the fragment file already exists with the final
   pattern shapes, in the order the header comment requires (the 4
   specific engine/Windows forms before the generic catch-all). Includes a
   first-match walk over the fragment's own blocks, in file order, for
   every one of the 6 SCOPE spoken forms -- the same resolution algorithm
   patterns.toml itself uses (first match wins, file order).

2. ORDERING HAZARD check -- proves the fragment header's claim that the
   generic search-web pattern, evaluated on its own out of the required
   order, WOULD swallow the 4 more specific forms and capture the wrong
   text. This documents why the fragment's internal order is load bearing;
   it is not a live-file check.

3. LIVE-FILE checks (against the real
   services/wheelhouse/speech/config/patterns.toml). These CANNOT pass
   until the boss splices every block from the fragment into that file, as
   a contiguous unit in the fragment's own order. Until that happens, every
   live-file assertion below fails with a "doc_id not found" message naming
   the missing doc_id -- that is the expected, correct pre-merge state. Do
   not edit patterns.toml to make these pass; the boss owns placement.

Also covered: the existing "search" (bare word, doc_id "search-selection")
command -- the x-ray-style command that Googles the current text
selection -- must be untouched, and none of this fragment's 6 forms may
already match anything in the live patterns.toml today (mandatory
duplicate-check evidence, wh-voice-access-parity.1.8: a throwaway script
compiled every live block with re.IGNORECASE and walked them in file order
with re.search; all 6 forms returned NO MATCH; the bare word "search"
matched only doc_id "search-selection", whose pattern requires nothing
after "search" so it cannot match any of the 6 forms here).
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[2]
_FRAGMENT_PATH = _REPO_ROOT / "patterns-staging" / "wh-voice-access-parity.1.8.toml"
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
        "patterns-staging/wh-voice-access-parity.1.8.toml into that file as "
        "a contiguous unit, in the fragment's own order -- see that "
        "fragment's header comment."
    )
    return by_id[doc_id]


def _compile(block: dict) -> re.Pattern:
    return re.compile(block["pattern"], re.IGNORECASE)


def _first_match_walk(blocks: list[dict], utterance: str) -> tuple[str, re.Match] | None:
    """Mirror the real matcher: first block (in file order) whose compiled
    pattern matches the utterance wins. re.search is used because it
    correctly reproduces both pattern kinds documented in patterns.toml's
    schema comment: a ^-anchored pattern still only matches at position 0
    (the same result re.match would give), and an unanchored pattern can
    match mid-utterance ("search mode")."""
    for block in blocks:
        rx = _compile(block)
        m = rx.search(utterance)
        if m:
            return block["doc_id"], m
    return None


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
# Expected shape table: one row per SCOPE form (6 spoken forms, 5 pattern
# blocks -- "search X" and "search for X" share one block, search-web,
# via a non-capturing (?: for)? alternation).
# ---------------------------------------------------------------------------

EXPECTED = [
    (
        "search-web-windows",
        [
            {"function": "hk", "params": ["win", "s"], "awaits_done": True},
            {"function": "type_text", "params": ["g1"]},
        ],
        [
            ("search windows for cats", "cats"),
            ("search windows for the weather", "the weather"),
        ],
    ),
    (
        "search-web-google",
        [{"function": "gs", "params": ["g1"]}],
        [("search on google for cats", "cats")],
    ),
    (
        "search-web-bing",
        [{"function": "open_url", "params": ["https://www.bing.com/search?q=g1"]}],
        [("search on bing for cats", "cats")],
    ),
    (
        "search-web-youtube",
        [
            {
                "function": "open_url",
                "params": ["https://www.youtube.com/results?search_query=g1"],
            }
        ],
        [("search on youtube for cats", "cats")],
    ),
    (
        "search-web",
        [{"function": "gs", "params": ["g1"]}],
        [("search cats", "cats"), ("search for cats", "cats")],
    ),
]

assert len(EXPECTED) == 5, "expected 5 pattern blocks covering 6 spoken forms"

# All 6 spoken forms in SCOPE order, each with its expected winning doc_id
# and captured g1 -- used by the first-match-walk tests below.
SPOKEN_FORMS = [
    ("search cats", "search-web", "cats"),
    ("search for cats", "search-web", "cats"),
    ("search windows for cats", "search-web-windows", "cats"),
    ("search on google for cats", "search-web-google", "cats"),
    ("search on bing for cats", "search-web-bing", "cats"),
    ("search on youtube for cats", "search-web-youtube", "cats"),
]

assert len(SPOKEN_FORMS) == 6, "bead SCOPE names about 6 forms"


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
        assert m.group(1) == expected_g1, (
            f"{doc_id}: {utterance!r} captured g1={m.group(1)!r}, expected {expected_g1!r}"
        )


def test_fragment_all_blocks_require_hotword(fragment_blocks):
    """Design decision recorded in the fragment header: every block here
    follows the same requires_hotword = true precedent as the existing
    "find" (^find\\s*(.*)$) and "select-phrase" (^select (.+)$) entries in
    patterns.toml, because an unanchored free-text capture after "search"
    risks matching an ordinary dictated sentence that happens to start with
    that word."""
    for block in fragment_blocks:
        assert block.get("requires_hotword") is True, (
            f"{block.get('doc_id')}: expected requires_hotword = true"
        )


def test_fragment_open_url_blocks_keep_capture_marker_out_of_scheme_and_host():
    """Mirrors command_engine.py's own safety check (open_url is in
    _URL_ENCODED_SUBSTITUTION_ACTIONS, which rejects a substitution marker
    found in the scheme/host part of the template): the site part of the
    address must be written out, not sourced from a capture group."""
    authority_re = re.compile(r"^([^:/?#]*://)([^/?#\\]*)")
    for doc_id in ("search-web-bing", "search-web-youtube"):
        block = _require_doc_id(
            _index_by_doc_id(_load_pattern_blocks(_FRAGMENT_PATH)), doc_id, _FRAGMENT_PATH
        )
        template = block["actions"][0]["params"][0]
        assert block["actions"][0]["function"] == "open_url"
        m = authority_re.match(template)
        assert m is not None, f"{doc_id}: template {template!r} has no scheme/host"
        authority = m.group(0)
        assert authority.lower().startswith(("http://", "https://")), (
            f"{doc_id}: template {template!r} must start with http:// or https://"
        )
        for marker in ("g1", "g2", "g3", "g4", "g5", "g6", "g7", "g8", "g9"):
            assert marker not in authority, (
                f"{doc_id}: capture marker {marker!r} found in scheme/host "
                f"{authority!r} of template {template!r} -- the site part "
                "must be written out, not sourced from a capture group"
            )


def test_fragment_search_web_windows_uses_hk_win_s_then_type_text(fragment_by_id):
    block = _require_doc_id(fragment_by_id, "search-web-windows", _FRAGMENT_PATH)
    hk_step, type_step = block["actions"]
    assert hk_step == {"function": "hk", "params": ["win", "s"], "awaits_done": True}
    assert type_step == {"function": "type_text", "params": ["g1"]}


def test_fragment_google_forms_use_existing_gs_action(fragment_by_id):
    for doc_id in ("search-web-google", "search-web"):
        block = _require_doc_id(fragment_by_id, doc_id, _FRAGMENT_PATH)
        assert block["actions"] == [{"function": "gs", "params": ["g1"]}], (
            f"{doc_id}: expected the sole action to be the existing gs action"
        )


@pytest.mark.parametrize("utterance,expected_doc_id,expected_g1", SPOKEN_FORMS)
def test_fragment_first_match_walk_resolves_every_spoken_form(
    utterance, expected_doc_id, expected_g1, fragment_blocks
):
    """First-match walk over the fragment's own blocks, in the fragment's
    file order -- the same algorithm patterns.toml itself uses. Proves the
    fragment's internal ordering (4 specific forms before the search-web
    catch-all) resolves every one of the 6 SCOPE forms to its intended
    doc_id today, inside the fragment alone."""
    result = _first_match_walk(fragment_blocks, utterance)
    assert result is not None, f"{utterance!r}: no fragment block matched"
    doc_id, m = result
    assert doc_id == expected_doc_id, (
        f"{utterance!r} resolved to doc_id {doc_id!r}, expected {expected_doc_id!r}"
    )
    assert m.group(1) == expected_g1, (
        f"{utterance!r} captured g1={m.group(1)!r}, expected {expected_g1!r}"
    )


# ---------------------------------------------------------------------------
# 2. Ordering hazard -- documents why the fragment's internal order (4
#    specific forms before the search-web catch-all) is load bearing.
# ---------------------------------------------------------------------------


def test_generic_search_web_pattern_would_swallow_specific_forms_if_misordered(fragment_by_id):
    """If search-web were ever moved ahead of the 4 specific blocks (or one
    of them were removed from patterns.toml later without removing this
    one), its own pattern -- evaluated alone -- matches every specific
    form's utterance too, capturing the wrong text (e.g. "windows for
    cats" instead of "cats"). This is exactly the hazard the fragment
    header's INTERNAL ORDERING note describes; it is why the boss must
    splice all 5 blocks in as a contiguous unit in the fragment's own
    order."""
    search_web = _require_doc_id(fragment_by_id, "search-web", _FRAGMENT_PATH)
    rx = _compile(search_web)
    misdirected = [
        ("search windows for cats", "windows for cats"),
        ("search on google for cats", "on google for cats"),
        ("search on bing for cats", "on bing for cats"),
        ("search on youtube for cats", "on youtube for cats"),
    ]
    for utterance, wrong_capture in misdirected:
        m = rx.search(utterance)
        assert m is not None, f"{utterance!r}: expected search-web to match on its own"
        assert m.group(1) == wrong_capture, (
            f"{utterance!r}: expected the hazard capture {wrong_capture!r}, got "
            f"{m.group(1)!r} -- if this fails because search-web now excludes "
            "these forms, the hazard note in the fragment header is stale and "
            "should be revisited, not because the hazard was silently fixed "
            "without updating the header"
        )


# ---------------------------------------------------------------------------
# 3. Live-file checks -- cannot pass until the boss splices this fragment's
#    blocks into services/wheelhouse/speech/config/patterns.toml as a
#    contiguous unit, in the fragment's own order.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc_id,expected_actions,samples", EXPECTED, ids=[e[0] for e in EXPECTED])
def test_live_patterns_toml_block_matches_and_acts(doc_id, expected_actions, samples, live_by_id):
    block = _require_doc_id(live_by_id, doc_id, _LIVE_PATTERNS_PATH)
    assert block["actions"] == expected_actions
    rx = _compile(block)
    for utterance, expected_g1 in samples:
        m = rx.match(utterance)
        assert m is not None, f"{doc_id}: pattern {block['pattern']!r} did not match {utterance!r}"
        assert m.group(1) == expected_g1


@pytest.mark.parametrize("utterance,expected_doc_id,expected_g1", SPOKEN_FORMS)
def test_live_patterns_toml_first_match_walk_resolves_every_spoken_form(
    utterance, expected_doc_id, expected_g1, live_blocks
):
    """Same first-match walk as the fragment-level test above, run against
    the live patterns.toml -- proves the placement the boss chooses keeps
    the internal ordering intact once these blocks sit among the other
    ~165 live blocks, not just in isolation."""
    result = _first_match_walk(live_blocks, utterance)
    assert result is not None, (
        f"{utterance!r}: no live block matched (expected pre-merge; the "
        "boss has not spliced this fragment in yet)"
    )
    doc_id, m = result
    assert doc_id == expected_doc_id, (
        f"{utterance!r} resolved to doc_id {doc_id!r} in the live file, "
        f"expected {expected_doc_id!r}"
    )
    assert m.group(1) == expected_g1


def test_live_patterns_toml_places_fragment_blocks_as_contiguous_unit_in_order(live_position):
    expected_order = [doc_id for doc_id, _actions, _samples in EXPECTED]
    positions = []
    for doc_id in expected_order:
        pos = live_position.get(doc_id)
        assert pos is not None, (
            f"doc_id {doc_id!r} not found in the live patterns.toml yet "
            "(expected pre-merge)."
        )
        positions.append(pos)
    assert positions == sorted(positions), (
        f"fragment doc_ids are not in the required relative order in the "
        f"live file: found position order {list(zip(expected_order, positions))}, "
        "expected search-web-windows, search-web-google, search-web-bing, "
        "search-web-youtube, then search-web last (see the fragment header's "
        "INTERNAL ORDERING note)."
    )
    contiguous_span = positions[-1] - positions[0] + 1
    assert contiguous_span == len(positions), (
        f"fragment doc_ids are not contiguous in the live file: positions "
        f"{positions} span {contiguous_span} slots for {len(positions)} blocks "
        "-- another block was interleaved between them, which can reopen the "
        "ordering hazard described in the fragment header"
    )


# ---------------------------------------------------------------------------
# Existing "search" (x-ray search-of-selection) command must be untouched,
# and none of this fragment's 6 forms may already match anything live today.
# ---------------------------------------------------------------------------


def test_existing_search_selection_command_untouched(live_by_id):
    """doc_id "search-selection" is the existing x-ray search command named
    in the bead's NOTE: it Googles the current text selection (copy, then
    gs on the clipboard capture) and is a different command from every
    form in this fragment. Its pattern '''^search$''' matches only the bare
    word with nothing after it ($ anchors immediately), so it cannot
    overlap with any of this fragment's 6 forms, all of which require text
    after "search"."""
    block = _require_doc_id(live_by_id, "search-selection", _LIVE_PATTERNS_PATH)
    assert block["pattern"] == "^search$"
    assert block.get("whole_utterance_only") is True
    assert block["actions"] == [
        {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
        {"function": "capture_clipboard", "params": [], "awaits_done": True},
        {"function": "gs", "params": ["capture_clipboard"]},
    ]
    rx = _compile(block)
    assert rx.search("search") is not None
    for utterance, _doc_id, _g1 in SPOKEN_FORMS:
        assert rx.search(utterance) is None, (
            f"existing search-selection pattern unexpectedly matches "
            f"{utterance!r} -- it should only match the bare word 'search'"
        )


def test_search_forms_first_match_their_own_entries_in_live_file(live_blocks):
    """Post-placement truth (fragment spliced 2026-08-24): every target
    spoken form now first-matches its own entry in the live file. The
    pre-placement version of this test was the mandatory duplicate check
    (wh-voice-access-parity.1.8) asserting no live block matched them."""
    for utterance, expected_doc_id, _expected_g1 in SPOKEN_FORMS:
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
