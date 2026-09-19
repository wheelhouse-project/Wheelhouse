"""Tests for wh-voice-access-parity.1.7 -- window management and per-app
commands.

The splice is done. This file now tests a standing split, not a pending
merge, and the two groups below have different subjects. It stays
self-contained on purpose (no `from speech...` imports), which is what let
it run from patterns-staging/ before the splice.

Two groups of checks:

1. FRAGMENT checks (against patterns-staging/wh-voice-access-parity.1.7.toml
   directly). The fragment is the RECORD of what was spliced in 2026-08 and
   is not edited afterwards. These checks are a standing sanity check on its
   regex and action shape.

2. LIVE-FILE checks (against the real
   services/wheelhouse/speech/config/patterns.toml). These run against
   _PLACED_EXPECTED, which is EXPECTED minus _NOT_IN_LIVE_FILE. A doc_id in
   _NOT_IN_LIVE_FILE is absent from the live file on purpose, and the comment
   above that list gives the three separate reasons (excluded before the
   splice, deleted, replaced by wh-keyboard-toggle-only). Absence is
   permanent for five of the six. It is NOT permanent for go-home: David
   has since ruled for its win+d meaning and the block is staged in
   patterns-staging/wh-voice-access-parity.1.7-go-home.toml, so when that
   splice happens go-home leaves _NOT_IN_LIVE_FILE and its live-file check
   starts running. The comment above _NOT_IN_LIVE_FILE carries the full
   reason for each of the three groups; read it before changing the list.

   Do NOT splice a fragment block into patterns.toml to make a live-file
   check pass. go-home is the one block expected to be spliced later, and
   that splice happens for David's ruling, paired with removing go-home
   from _NOT_IN_LIVE_FILE -- never to turn a failing check green.
   Four of the six absent doc_ids are the show and hide keyboard
   commands David removed on 2026-08-27; re-splicing them would restore
   commands that promise a direction the keystroke cannot deliver, which is
   the defect wh-keyboard-toggle-only removed. If a live-file check fails for
   a doc_id NOT in _NOT_IN_LIVE_FILE, that is a real regression in
   patterns.toml.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[2]
_FRAGMENT_PATH = _REPO_ROOT / "patterns-staging" / "wh-voice-access-parity.1.7.toml"
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
        f"doc_id {doc_id!r} not found in {source}. The splice is done, so "
        "when source is the live patterns.toml this is a REGRESSION in that "
        "file, not a pending merge. The only doc_ids expected to be absent "
        "from the live file are the six in _NOT_IN_LIVE_FILE, and no "
        "live-file check asks for those. Do NOT splice a block from "
        "patterns-staging/wh-voice-access-parity.1.7.toml to make this pass "
        "-- the module docstring explains why. Find out what removed the "
        "block instead."
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
def fragment_position(fragment_blocks) -> dict[str, int]:
    return _index_position(fragment_blocks)


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
# Expected shape table: one row per pattern block (20 blocks, covering 23
# SCOPE spoken forms -- switch-to-app covers 2 forms and close-app covers 3
# via a non-capturing alternation).
#
# Each row: doc_id, expected actions, and sample (utterance, expected g1)
# pairs. g1 is None where the block takes no capture group.
# ---------------------------------------------------------------------------

_HK = "hk"
_ACTIVATE = "activate"

EXPECTED = [
    # -- window management: bulk and fixed-phrase forms --
    (
        "go-to-desktop",
        [{"function": _HK, "params": ["win", "d"]}],
        [("go to desktop", None)],
    ),
    (
        "go-home",
        [{"function": _HK, "params": ["win", "d"]}],
        [("go home", None)],
    ),
    (
        "restore-window",
        [{"function": _HK, "params": ["win", "down"]}],
        [("restore window", None)],
    ),
    (
        "show-task-switcher",
        [{"function": _HK, "params": ["win", "tab"]}],
        [("show task switcher", None)],
    ),
    (
        "list-all-windows",
        [{"function": _HK, "params": ["win", "tab"]}],
        [("list all windows", None)],
    ),
    (
        "show-all-windows",
        [{"function": _HK, "params": ["win", "tab"]}],
        [("show all windows", None)],
    ),
    (
        "minimize-all-windows",
        [{"function": _HK, "params": ["win", "m"]}],
        [("minimize all windows", None)],
    ),
    # -- snap window: four separate patterns, one per direction --
    (
        "snap-window-left",
        [{"function": _HK, "params": ["win", "left"]}],
        [("snap window to the left", None)],
    ),
    (
        "snap-window-right",
        [{"function": _HK, "params": ["win", "right"]}],
        [("snap window to the right", None)],
    ),
    (
        "snap-window-top",
        [{"function": _HK, "params": ["win", "alt", "up"]}],
        [("snap window to the top", None)],
    ),
    (
        "snap-window-bottom",
        [{"function": _HK, "params": ["win", "alt", "down"]}],
        [("snap window to the bottom", None)],
    ),
    # -- touch/on-screen keyboard --
    (
        "show-touch-keyboard",
        [{"function": _HK, "params": ["win", "ctrl", "o"]}],
        [("show touch keyboard", None)],
    ),
    (
        "show-keyboard",
        [{"function": _HK, "params": ["win", "ctrl", "o"]}],
        [("show keyboard", None)],
    ),
    (
        "hide-touch-keyboard",
        [{"function": _HK, "params": ["win", "ctrl", "o"]}],
        [("hide touch keyboard", None)],
    ),
    (
        "hide-keyboard",
        [{"function": _HK, "params": ["win", "ctrl", "o"]}],
        [("hide keyboard", None)],
    ),
    # -- per-app forms --
    (
        "switch-to-app",
        [{"function": _ACTIVATE, "params": ["g1"]}],
        [
            ("switch to notepad", "notepad"),
            ("switch to chrome.exe", "chrome.exe"),
            ("go to notepad", "notepad"),
        ],
    ),
    (
        "show-app",
        [{"function": _ACTIVATE, "params": ["g1"]}],
        [("show notepad", "notepad")],
    ),
    (
        "close-app",
        [
            {"function": _ACTIVATE, "params": ["g1"], "awaits_done": True},
            {"function": _HK, "params": ["alt", "f4"], "awaits_done": True},
        ],
        [
            ("close notepad", "notepad"),
            ("exit notepad", "notepad"),
            ("quit notepad", "notepad"),
        ],
    ),
    (
        "minimize-app",
        [
            {"function": _ACTIVATE, "params": ["g1"], "awaits_done": True},
            {"function": _HK, "params": ["win", "down"], "awaits_done": True},
        ],
        [("minimize notepad", "notepad")],
    ),
    (
        "maximize-app",
        [
            {"function": _ACTIVATE, "params": ["g1"], "awaits_done": True},
            {"function": _HK, "params": ["win", "up"], "awaits_done": True},
        ],
        [("maximize notepad", "notepad")],
    ),
]

assert len(EXPECTED) == 20, "expected 20 pattern blocks for this bead's SCOPE"

# 24 sample utterances covering 23 spoken forms: switch-to-app carries the
# two wordings (switch to / go to) plus one extra executable-name capture
# sample ("switch to chrome.exe"), which is a capture-shape check, not a
# distinct spoken form. The original 23 here conflated forms with samples
# and failed at collection time (boss placement fix, 2026-08-24).
_TOTAL_SPOKEN_FORMS = sum(len(samples) for _doc_id, _actions, samples in EXPECTED)
assert _TOTAL_SPOKEN_FORMS == 24, (
    f"expected 24 sample utterances (23 spoken forms + 1 capture-shape "
    f"sample), counted {_TOTAL_SPOKEN_FORMS}"
)

_PER_APP_HOTWORD_DOC_IDS = {
    "switch-to-app",
    "show-app",
    "close-app",
    "minimize-app",
    "maximize-app",
}

# Blocks whose pattern MUST precede a given other block within this same
# fragment (ORDERING HAZARD #2 in the fragment header) -- (earlier, later)
# pairs, both directions of "earlier must come before later".
_INTERNAL_ORDER_PAIRS = [
    ("go-to-desktop", "switch-to-app"),
    ("go-home", "switch-to-app"),
    ("minimize-all-windows", "minimize-app"),
    ("show-task-switcher", "show-app"),
    ("list-all-windows", "show-app"),
    ("show-all-windows", "show-app"),
    ("show-touch-keyboard", "show-app"),
    ("show-keyboard", "show-app"),
]

# Six doc_ids appear in this fragment but NOT in the live patterns.toml,
# for three DIFFERENT reasons. Keep the reasons apart: a reader who merges
# them will draw the wrong conclusion about one block or another.
#
# go-home was EXCLUDED from placement (boss decision 2026-08-24): "go home"
# is a working caret command through cursor-navigate -> NavigationParser ->
# the Home key, and it is the documented example on the cursor-navigate
# entry. The staged win+d meaning replaces live behavior, which only the
# project owner can approve. David has since ruled for win+d, and the block
# is staged in patterns-staging/wh-voice-access-parity.1.7-go-home.toml,
# but it is NOT spliced into the live file yet.
#
# go-to-desktop WAS placed and has now been DELETED from the live file.
# David eliminated it on 2026-08-24 (wh-voice-access-parity.3.3) because
# "go home" becomes the one show-desktop command. This fragment keeps the
# block as the record of what was spliced at the time, so the fragment
# checks above still cover it. In the live file the accepted consequence is
# that "go to desktop" reaches switch-to-app and captures "desktop" as an
# application name; the guard for that is TestGoToDesktopIsEliminated in
# tests/test_va_navigation_router.py.
# The four keyboard blocks were REPLACED, not deleted. win+ctrl+o is a
# toggle, so a "show" or "hide" phrase promised a direction the keystroke
# cannot deliver -- "hide keyboard" opened the keyboard whenever it was
# already closed. David ruled on 2026-08-27 (wh-keyboard-toggle-only) to
# drop the show and hide modifiers, and the live file now carries the
# single doc_id keyboard-toggle ('^keyboard$') in their place. An
# intermediate commit (5b2f2f29) briefly used toggle-touch-keyboard and
# toggle-keyboard; 7e5d6272 replaced both with the bare word, so NEITHER
# of those doc_ids is in the live file and failing to find one there does
# NOT mean the change reverted. Section 3 near the bottom of this file
# checks keyboard-toggle and pins the two "toggle"-prefixed phrases
# absent. It is a separate section because none of it is in the fragment:
# this fragment stays the record of what was spliced in 2026-08.
_NOT_IN_LIVE_FILE = (
    "go-home",
    "go-to-desktop",
    "show-touch-keyboard",
    "show-keyboard",
    "hide-touch-keyboard",
    "hide-keyboard",
)
_PLACED_EXPECTED = [row for row in EXPECTED if row[0] not in _NOT_IN_LIVE_FILE]
_LIVE_ORDER_PAIRS = [
    pair for pair in _INTERNAL_ORDER_PAIRS
    if not any(doc_id in pair for doc_id in _NOT_IN_LIVE_FILE)
]


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
        has_g1 = any(p == "g1" for step in expected_actions for p in step.get("params", []))
        if has_g1:
            assert m.group(1) == expected_g1, (
                f"{doc_id}: {utterance!r} captured g1={m.group(1)!r}, expected {expected_g1!r}"
            )


def test_fragment_per_app_blocks_require_hotword(fragment_by_id):
    """Every per-app block is a bare 'verb + capture-everything' pattern and
    must gate on the hotword, matching the activate-app shape model (bead
    header comment, requires_hotword note)."""
    for doc_id in _PER_APP_HOTWORD_DOC_IDS:
        block = fragment_by_id[doc_id]
        assert block.get("requires_hotword") is True, (
            f"{doc_id}: expected requires_hotword = true (bare capture-all pattern)"
        )


def test_fragment_window_and_keyboard_blocks_have_no_capture_group(fragment_by_id):
    """The 15 window-bulk/snap/keyboard blocks are complete fixed phrases
    with no capture group and no hotword requirement (see the
    requires_hotword note in the fragment header)."""
    fixed_phrase_doc_ids = [
        doc_id for doc_id, _actions, _samples in EXPECTED
        if doc_id not in _PER_APP_HOTWORD_DOC_IDS
    ]
    assert len(fixed_phrase_doc_ids) == 15
    for doc_id in fixed_phrase_doc_ids:
        block = fragment_by_id[doc_id]
        compiled = re.compile(block["pattern"])
        assert compiled.groups == 0, (
            f"{doc_id}: fixed-phrase block should take no capture group, "
            f"found {compiled.groups} in {block['pattern']!r}"
        )
        assert not block.get("requires_hotword"), (
            f"{doc_id}: fixed-phrase window/keyboard block should not require the hotword"
        )


def test_fragment_snap_directions_are_four_separate_patterns(fragment_by_id):
    """Bead MECHANISM: hk takes literal key names and cannot map a captured
    direction word to a key, so the four snap directions must be four
    separate [[pattern]] blocks, not one block with a direction capture
    group."""
    snap_doc_ids = [
        "snap-window-left",
        "snap-window-right",
        "snap-window-top",
        "snap-window-bottom",
    ]
    seen_patterns = set()
    for doc_id in snap_doc_ids:
        block = _require_doc_id(fragment_by_id, doc_id, _FRAGMENT_PATH)
        compiled = re.compile(block["pattern"])
        assert compiled.groups == 0, (
            f"{doc_id}: snap pattern must not capture a direction word, "
            f"found {compiled.groups} group(s) in {block['pattern']!r}"
        )
        assert block["pattern"] not in seen_patterns, (
            f"{doc_id}: pattern {block['pattern']!r} duplicates another snap direction's pattern"
        )
        seen_patterns.add(block["pattern"])
    assert len(seen_patterns) == 4


def test_fragment_keyboard_help_text_says_on_screen_not_touch(fragment_blocks):
    """Bead KEYBOARD APPROXIMATION, load bearing for help text: must say
    'on-screen keyboard', never 'touch keyboard', anywhere help text is
    derived from these four blocks. This fragment carries that wording only
    in its header comment (no doc_id-level 'help' field exists in this
    schema today), so assert directly on the fragment source text."""
    text = _require_fragment().read_text(encoding="utf-8")
    assert "on-screen keyboard" in text.lower(), (
        "fragment must record the on-screen-keyboard approximation in its header comment"
    )


def test_fragment_records_friendly_name_limit():
    """Bead LIMIT, load bearing: the friendly-name-vs-executable/title gap
    must be recorded as a fragment comment."""
    text = _require_fragment().read_text(encoding="utf-8")
    assert "friendly" in text.lower() and "LIMIT" in text, (
        "fragment must record the friendly-app-name limit as a comment"
    )


def test_fragment_header_states_placement_above_cursor_navigate():
    text = _require_fragment().read_text(encoding="utf-8")
    match = re.search(r"(?m)^\[\[pattern\]\]", text)
    assert match is not None, "fragment has no [[pattern]] table at all"
    header = text[: match.start()]
    assert "ABOVE" in header and "cursor-navigate" in header, (
        "fragment header must state that every go-prefixed entry goes above "
        "the cursor-navigate entry (acceptance criteria for "
        "wh-voice-access-parity.1.7)"
    )


def test_fragment_internal_ordering_is_specific_before_general(fragment_position):
    for earlier_id, later_id in _INTERNAL_ORDER_PAIRS:
        earlier_pos = fragment_position[earlier_id]
        later_pos = fragment_position[later_id]
        assert earlier_pos < later_pos, (
            f"{earlier_id!r} (position {earlier_pos}) must come before "
            f"{later_id!r} (position {later_pos}) in this fragment -- "
            "ORDERING HAZARD #2 in the fragment header"
        )


def _first_match_doc_id(live_blocks, utterance: str) -> str | None:
    for block in live_blocks:
        rx = _compile(block)
        if rx.search(utterance):
            return block.get("doc_id")
    return None


def test_go_prefixed_forms_resolve_post_placement(live_blocks):
    """Post-placement resolution of the go-prefixed forms.

    'go to <app>' reaches switch-to-app, which sits above cursor-navigate.

    'go to desktop' now reaches switch-to-app TOO, and captures "desktop"
    as an application name. That is David's elimination ruling of
    2026-08-24 (wh-voice-access-parity.3.3): the fixed go-to-desktop entry
    is deleted, and the capture claiming the phrase is the consequence he
    accepted.

    'go home' STAYS on cursor-navigate because its go-home block is not
    spliced into the live file yet.
    """
    for utterance, want in (
        ("go to desktop", "switch-to-app"),
        ("go home", "cursor-navigate"),
        ("go to notepad", "switch-to-app"),
    ):
        got = _first_match_doc_id(live_blocks, utterance)
        assert got == want, (
            f"{utterance!r} first-matches {got!r} in the live file, expected "
            f"{want!r} -- placement order around cursor-navigate has drifted"
        )


def test_scope_forms_first_match_their_own_entries_in_live_file(live_blocks):
    """Post-placement replacement of the pre-merge DUPLICATE CHECK: every
    non-go SCOPE form must now first-match its OWN doc_id -- nothing earlier
    in the file may claim it, and the form must not fall through unmatched."""
    form_to_doc_id = [
        ("restore window", "restore-window"),
        ("show task switcher", "show-task-switcher"),
        ("list all windows", "list-all-windows"),
        ("show all windows", "show-all-windows"),
        ("minimize all windows", "minimize-all-windows"),
        ("snap window to the left", "snap-window-left"),
        ("snap window to the right", "snap-window-right"),
        ("snap window to the top", "snap-window-top"),
        ("snap window to the bottom", "snap-window-bottom"),
        ("switch to notepad", "switch-to-app"),
        ("close notepad", "close-app"),
        ("exit notepad", "close-app"),
        ("quit notepad", "close-app"),
        ("minimize notepad", "minimize-app"),
        ("maximize notepad", "maximize-app"),
        ("show notepad", "show-app"),
        # The four keyboard phrases are gone from the live file; what they
        # reach now is pinned in
        # test_live_file_no_longer_answers_the_show_and_hide_keyboard_phrases.
    ]
    for utterance, want in form_to_doc_id:
        got = _first_match_doc_id(live_blocks, utterance)
        assert got == want, (
            f"{utterance!r} first-matches {got!r} in the live file, "
            f"expected its own entry {want!r}"
        )


# ---------------------------------------------------------------------------
# 2. Live-file checks -- against the real
#    services/wheelhouse/speech/config/patterns.toml. The splice is done
#    and these pass. They are the standing guard that the spliced blocks
#    are still present, still above cursor-navigate, and still in this
#    fragment's internal order. A failure here is a regression in
#    patterns.toml, not a pending merge.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc_id,expected_actions,samples",
    _PLACED_EXPECTED,
    ids=[e[0] for e in _PLACED_EXPECTED],
)
def test_live_patterns_toml_block_matches_and_acts(doc_id, expected_actions, samples, live_by_id):
    block = _require_doc_id(live_by_id, doc_id, _LIVE_PATTERNS_PATH)
    assert block["actions"] == expected_actions
    rx = _compile(block)
    for utterance, expected_g1 in samples:
        m = rx.match(utterance)
        assert m is not None, f"{doc_id}: pattern {block['pattern']!r} did not match {utterance!r}"
        has_g1 = any(p == "g1" for step in expected_actions for p in step.get("params", []))
        if has_g1:
            assert m.group(1) == expected_g1


def test_live_patterns_toml_places_go_prefixed_forms_above_cursor_navigate(live_position):
    cursor_navigate_pos = live_position.get("cursor-navigate")
    assert cursor_navigate_pos is not None, (
        "doc_id 'cursor-navigate' not found in the live patterns.toml -- "
        "has it been renamed or removed? This bead must not touch it."
    )
    # go-home and go-to-desktop are both absent from the live file, for the
    # two different reasons given above.
    go_prefixed_doc_ids = ["switch-to-app"]
    for doc_id in go_prefixed_doc_ids:
        pos = live_position.get(doc_id)
        assert pos is not None, (
            f"doc_id {doc_id!r} is missing from the live patterns.toml. "
            "The splice is done, so this is a regression in that file, not "
            "a pending merge."
        )
        assert pos < cursor_navigate_pos, (
            f"{doc_id!r} is at position {pos}, cursor-navigate is at "
            f"{cursor_navigate_pos} -- {doc_id!r} must come BEFORE "
            "cursor-navigate or cursor-navigate's ^go (.+) pattern will "
            "swallow it first (first match wins, patterns.toml ORDERING RULES)."
        )


def test_live_patterns_toml_respects_internal_ordering(live_position):
    for earlier_id, later_id in _LIVE_ORDER_PAIRS:
        earlier_pos = live_position.get(earlier_id)
        later_pos = live_position.get(later_id)
        assert earlier_pos is not None and later_pos is not None, (
            f"{earlier_id!r} or {later_id!r} is missing from the live "
            "patterns.toml. The splice is done, so this is a regression in "
            "that file, not a pending merge."
        )
        assert earlier_pos < later_pos, (
            f"{earlier_id!r} (position {earlier_pos}) must come before "
            f"{later_id!r} (position {later_pos}) in the live file -- "
            "ORDERING HAZARD #2 in the fragment header"
        )


def test_live_patterns_toml_places_close_app_below_close_window(live_position):
    """ORDERING HAZARD #3: close-app's pattern also matches the bare phrase
    'close window', already owned by the existing close-window doc_id.
    close-app must sit below it."""
    close_window_pos = live_position.get("close-window")
    assert close_window_pos is not None, (
        "doc_id 'close-window' not found in the live patterns.toml -- has it "
        "been renamed or removed? This bead must not touch it."
    )
    close_app_pos = live_position.get("close-app")
    assert close_app_pos is not None, (
        "doc_id 'close-app' is missing from the live patterns.toml. The "
        "splice is done, so this is a regression in that file, not a "
        "pending merge."
    )
    assert close_window_pos < close_app_pos, (
        f"close-window (position {close_window_pos}) must come before "
        f"close-app (position {close_app_pos}) -- ORDERING HAZARD #3 in the "
        "fragment header"
    )


# ---------------------------------------------------------------------------
# Existing "go" trigger (cursor-navigate) and "close window" trigger
# (close-window) must still match their own entries once this fragment is
# spliced in above/around them.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3. The keyboard toggle (wh-keyboard-toggle-only) -- live file only.
#
#    This one block REPLACES the fragment's four show/hide keyboard blocks.
#    It is deliberately absent from EXPECTED: EXPECTED is the fragment's
#    shape table, the fragment is not edited, and the four retired doc_ids
#    are listed in _NOT_IN_LIVE_FILE above.
# ---------------------------------------------------------------------------

_TOGGLE_KEYBOARD = [
    ("keyboard-toggle", "keyboard"),
]

# Live for one commit only (5b2f2f29) and then replaced. David ruled on
# 2026-08-27 that the word "keyboard" IS the toggle command, so the
# "toggle" prefix went the way of the show and hide ones. These two are
# pinned absent so nobody restores them from the commit history.
_RETIRED_TOGGLE_PHRASES = ("toggle keyboard", "toggle touch keyboard")

_RETIRED_KEYBOARD_DOC_IDS = (
    "show-touch-keyboard",
    "show-keyboard",
    "hide-touch-keyboard",
    "hide-keyboard",
)


def test_live_keyboard_toggle_blocks_send_the_toggle_keystroke(live_by_id):
    """The surviving phrase runs the same Win+Ctrl+O the retired ones ran.

    The keystroke never changed; only the words did. It is one word now:
    David ruled on 2026-08-27 that "the word 'keyboard' is the toggle
    command for the keyboard".
    """
    for doc_id, phrase in _TOGGLE_KEYBOARD:
        block = _require_doc_id(live_by_id, doc_id, _LIVE_PATTERNS_PATH)
        assert block["actions"] == [
            {"function": _HK, "params": ["win", "ctrl", "o"]}
        ], f"{doc_id}: actions {block['actions']!r}"
        assert block.get("whole_utterance_only") is True, (
            f"{doc_id}: must fire only as the whole utterance, like the four "
            "blocks it replaces"
        )
        rx = _compile(block)
        assert rx.groups == 0, f"{doc_id}: fixed phrase must take no capture group"
        assert rx.match(phrase) is not None, (
            f"{doc_id}: pattern {block['pattern']!r} did not match {phrase!r}"
        )


def test_live_keyboard_toggle_phrases_first_match_their_own_entries(live_blocks):
    """Nothing earlier in the file claims the word.

    Measured on 2026-08-27 over all 18 capture-all patterns in
    patterns.toml: none begins with "keyboard", so this block carries no
    ordering constraint in either direction. The old ORDERING HAZARD #2
    (show-touch-keyboard and show-keyboard above show-app) does not carry
    over to it.
    """
    for doc_id, phrase in _TOGGLE_KEYBOARD:
        got = _first_match_doc_id(live_blocks, phrase)
        assert got == doc_id, (
            f"{phrase!r} first-matches {got!r} in the live file, expected {doc_id!r}"
        )


def test_live_keyboard_toggle_is_deliberately_hotword_free(live_by_id):
    """The bare word fires without the hotword, and that is David's ruling.

    The block sets no requires_hotword, so it inherits the default false
    and "keyboard" spoken on its own toggles the on-screen keyboard --
    whole_utterance_only does not gate on the hotword, it only delays
    execution to the end of the utterance. The four retired show/hide
    phrases were hotword-free too, but two-word instructions almost never
    collide with dictation and a common noun does. David was shown that
    consequence, including that win+ctrl+o is a blind toggle no accident
    corrects, and ruled on 2026-08-27 to keep the command hotword-free
    (wh-keyboard-toggle-only.1.3). This pins the flag: before this test
    nothing in the file recorded it either way, so an edit could have
    flipped it without a failure. See the deliberate-choice comment above
    the block in patterns.toml.
    """
    for doc_id, _phrase in _TOGGLE_KEYBOARD:
        block = _require_doc_id(live_by_id, doc_id, _LIVE_PATTERNS_PATH)
        assert not block.get("requires_hotword"), (
            f"{doc_id}: requires_hotword is set. David ruled on 2026-08-27 "
            "to keep the bare word hotword-free; do not add the flag "
            "without asking (wh-keyboard-toggle-only.1.3)."
        )


def test_the_toggle_prefixed_keyboard_phrases_are_gone_too(live_blocks):
    """The one-commit "toggle keyboard" pair reaches nothing.

    5b2f2f29 added '^toggle keyboard$' and '^toggle touch keyboard$'.
    David replaced both with the bare word on 2026-08-27, so a user who
    learnt the older wording gets no command rather than a wrong one. No
    pattern in the file starts with "toggle" and no capture-all claims
    the phrase, so the utterance falls through to dictation.
    """
    for phrase in _RETIRED_TOGGLE_PHRASES:
        got = _first_match_doc_id(live_blocks, phrase)
        assert got is None, f"{phrase!r} unexpectedly matches {got!r}"


def test_live_file_no_longer_carries_the_show_and_hide_keyboard_entries(live_by_id):
    for doc_id in _RETIRED_KEYBOARD_DOC_IDS:
        assert doc_id not in live_by_id, (
            f"{doc_id!r} is still in the live patterns.toml -- "
            "wh-keyboard-toggle-only removed all four show/hide keyboard blocks"
        )


def test_live_file_no_longer_answers_the_show_and_hide_keyboard_phrases(live_blocks):
    """What the four retired phrases reach now, stated rather than left to
    a reader to work out.

    'hide ...' reaches nothing at all, but not because the word is unused:
    the file has two hide-prefixed patterns, '^(?:hide|dismiss) numbers$'
    and '^(?:hide|dismiss) grid$'. Both are fixed phrases, so neither can
    match a keyboard phrase, and no capture-all begins with 'hide'. A
    maintainer adding a hide-prefixed command is therefore not creating a
    fresh first-word key, and must check these two entries rather than
    assume the word is free. 'show ...' falls into show-app's
    '^show\\s+(.+)$' capture and
    would try to activate an application called "keyboard" or "touch
    keyboard" -- and only after the hotword, since show-app sets
    requires_hotword. That is the same accepted consequence "go to desktop"
    already has against switch-to-app, and it is why the phrases had to be
    removed rather than left to promise a direction.
    """
    for phrase in ("hide touch keyboard", "hide keyboard"):
        got = _first_match_doc_id(live_blocks, phrase)
        assert got is None, f"{phrase!r} unexpectedly matches {got!r}"
    for phrase in ("show touch keyboard", "show keyboard"):
        got = _first_match_doc_id(live_blocks, phrase)
        assert got == "show-app", (
            f"{phrase!r} first-matches {got!r}, expected the show-app capture"
        )


def test_existing_cursor_navigate_still_matches_non_scope_go_utterances(live_by_id):
    block = _require_doc_id(live_by_id, "cursor-navigate", _LIVE_PATTERNS_PATH)
    rx = _compile(block)
    for utterance, expected_g1 in [
        ("go left two words", "left two words"),
        ("go start of word", "start of word"),
    ]:
        m = rx.match(utterance)
        assert m is not None, f"cursor-navigate no longer matches {utterance!r}"
        assert m.group(1) == expected_g1


def test_existing_close_window_still_matches(live_by_id):
    block = _require_doc_id(live_by_id, "close-window", _LIVE_PATTERNS_PATH)
    rx = _compile(block)
    m = rx.match("close window")
    assert m is not None, "close-window no longer matches 'close window'"
    assert block.get("requires_hotword") is True
    assert block["actions"] == [{"function": "hk", "params": ["alt", "f4"]}]
