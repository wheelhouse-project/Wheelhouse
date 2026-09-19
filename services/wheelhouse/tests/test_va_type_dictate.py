"""Speech pattern tests for bd bead wh-voice-access-parity.1.9 (Voice Access
dictation escape and bare key commands).

STAGED FRAGMENT -- not yet part of the test suite. Two different kinds of
test live here, and they behave differently today on purpose:

1. TestDuplicateCheckAgainstLiveFile walks
   services/wheelhouse/speech/config/patterns.toml exactly the way the
   bead's mandatory first step requires (tomllib -> compile every block
   with re.IGNORECASE -> first-match-in-file-order wins) and checks it
   against production. Its "escape already exists" test PASSES today. Its
   parametrized "new forms resolve once placed" test FAILS today for type/
   dictate/enter/tab, because patterns-staging/wh-voice-access-parity.1.9.toml
   is not in the live file yet -- that is expected and correct, and it will
   start passing once the boss places all three blocks from that fragment
   as NEW entries. Do not edit patterns.toml from this file to make it
   pass; placement is the boss's decision, not this bead's authoring step.

   The same class also holds test_ordinary_sentences_never_reach_the_new
   _block, which passes today and must keep passing after placement.

2. TestTypeAndDictateAliases, TestBareEnterAndTab and TestSubmitGuard build
   their OWN self-contained temporary patterns.toml (via pytest's tmp_path,
   the same technique already shipped in
   services/wheelhouse/tests/test_speech_processor_trailing_command.py for
   this exact epic) using the literal TOML text of the blocks staged in
   wh-voice-access-parity.1.9.toml. These tests exercise the fragment's own
   correctness in isolation and DO pass today -- they never touch the live
   production file, so nothing here makes patterns.toml pass early. A
   dedicated consistency test (test_embedded_blocks_match_staged_fragment)
   parses the .toml fragment via tomllib and asserts the embedded copies
   below are byte-identical to the staged pattern/doc_id/actions, so the two
   cannot silently drift apart.

SCOPE covered here (bd show wh-voice-access-parity.1.9): "type X" and
"dictate X" as new leading aliases of the existing literal-bypass entry;
bare "enter" and bare "tab" as single-key press commands.

REVISED 2026-08-24 after a boss DECISION. The first version of this bead
replaced the shipped literal-bypass block with a widened, word-boundary-
guarded pattern. That was wrong: the \\b guard stops a match inside
"prototype" but not "type" as an ordinary whole word later in a sentence,
so "what type of coffee do you want" collapsed to "what" plus a literally-
typed "of coffee do you want". The shipped block is now left untouched and
"type"/"dictate" get their own ^-anchored block, doc_id
"type-dictate-bypass". Two tests here exist specifically to stop the
rejected version from returning: test_ordinary_sentences_never_reach_the
_new_block and test_ordinary_sentences_dictate_word_for_word.

OUT OF SCOPE / duplicate check: bare "escape" is also named in the bead's
SCOPE list, but it already exists in patterns.toml (doc_id "escape",
pattern ^escape$, lines 305-309). Verified by the compiled-regex
first-match walk in TestDuplicateCheckAgainstLiveFile below -- not a
substring/text search, because patterns are regexes (a body like
\\bparagraph ?sign\\b never contains the literal text it matches; the bead
DESCRIPTION records that a previous check using substring matching wrongly
reported 14 entries missing for this reason). Not re-authored here.
"""

import asyncio
import re
import sys
import tomllib
from pathlib import Path

import pytest

# patterns-staging/ sits directly under the worktree root -- one level above
# this file, mirroring patterns-staging/test_wh-voice-access-parity.1.2.py's
# own path setup. service_dir is added too so `from tests.test_speech_pipeline
# import SpeechPipelineHarness` resolves the same way it does for every test
# module that already lives under services/wheelhouse/tests/.
project_root = Path(__file__).resolve().parents[3]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
service_dir = project_root / "services" / "wheelhouse"
if str(service_dir) not in sys.path:
    sys.path.insert(1, str(service_dir))

from tests.test_speech_pipeline import SpeechPipelineHarness  # noqa: E402

PATTERNS_FILE = service_dir / "speech" / "config" / "patterns.toml"
STAGED_FRAGMENT = Path(__file__).resolve().parents[3] / "patterns-staging" / "wh-voice-access-parity.1.9.toml"

_TOML_HEADER = 'COMMAND_HOTWORD = "x-ray"\n\n'


def _production_pattern_entries():
    """Load the pattern entries straight from the live TOML file.

    Independent of the PatternCatalog wrapper so a catalog-loading bug
    cannot mask a missing or misshapen entry. Matches the helper already
    used in patterns-staging/test_wh-voice-access-parity.1.2.py.
    """
    with PATTERNS_FILE.open("rb") as handle:
        return tomllib.load(handle)["pattern"]


def _first_match_doc_id(entries, spoken_phrase):
    """Walk ``entries`` IN FILE ORDER, compiling each with re.IGNORECASE,
    and return the doc_id of the first block whose pattern matches
    ``spoken_phrase`` (via search(), the same "no ^ anchor = search mode"
    semantics documented at the top of patterns.toml). Returns None if no
    block matches -- this is the mandatory compiled-regex duplicate-check
    methodology, not a substring search.
    """
    for entry in entries:
        compiled = re.compile(entry["pattern"], re.IGNORECASE)
        if compiled.search(spoken_phrase):
            return entry.get("doc_id")
    return None


def _staged_fragment_entries():
    """Load the pattern entries from this bead's own staged .toml fragment."""
    if not STAGED_FRAGMENT.is_file():
        pytest.skip("development-only patterns-staging input is absent from this checkout")
    with STAGED_FRAGMENT.open("rb") as handle:
        return tomllib.load(handle)["pattern"]


# ============================================================================
# Mandatory first step: compiled-regex duplicate check against the LIVE file
# ============================================================================

class TestDuplicateCheckAgainstLiveFile:
    """Reproduces the bead's mandatory compiled-regex first-match walk over
    services/wheelhouse/speech/config/patterns.toml, as a permanent
    regression check rather than a one-off script.
    """

    def test_escape_already_exists(self):
        """"escape" must already resolve to the existing doc_id "escape"
        before this bead changes anything -- proves it is a genuine
        duplicate, not something this fragment needs to add. This test
        PASSES today.
        """
        entries = _production_pattern_entries()
        assert _first_match_doc_id(entries, "escape") == "escape"

    @pytest.mark.parametrize(
        ("spoken_phrase", "expected_doc_id"),
        (
            ("type hello", "type-dictate-bypass"),
            ("dictate hello", "type-dictate-bypass"),
            ("enter", "enter"),
            ("tab", "tab"),
        ),
    )
    def test_new_forms_resolve_once_placed(self, spoken_phrase, expected_doc_id):
        """These four forms currently resolve to NO MATCH (None) against the
        live file -- verified by the authoring executor's compiled-regex
        duplicate check before this fragment was written. This test FAILS
        today and is expected to; it starts passing once the boss places
        BLOCK 1 (replacing the current first pattern) and BLOCKS 2-3 (new
        entries) from wh-voice-access-parity.1.9.toml into
        services/wheelhouse/speech/config/patterns.toml.
        """
        entries = _production_pattern_entries()
        assert _first_match_doc_id(entries, spoken_phrase) == expected_doc_id

    @pytest.mark.parametrize(
        "spoken_phrase",
        (
            "what type of coffee do you want",
            "i will type this later",
            "please dictate the address",
            "she studies the prototype design",
        ),
    )
    def test_ordinary_sentences_never_reach_the_new_block(self, spoken_phrase):
        """The reason BLOCK 1 is ^-anchored, as a permanent check.

        The first version of this fragment widened the SHIPPED
        literal-bypass block to '''\\b(?:literal|type|dictate) (.+)$'''. The
        \\b guard stops a match inside "prototype", but it does nothing
        about "type" as an ordinary whole word later in a sentence: the
        rejected pattern turned "what type of coffee do you want" into the
        single word "what" plus a literally-typed "of coffee do you want".

        This test walks the live file plus the staged fragment and asserts
        that none of these four ordinary sentences resolves to the new
        block. It PASSES today, because the four sentences reach no
        command at all, and it must keep passing after placement. Restore
        the rejected \\b pattern and the first three sentences fail.
        """
        entries = _production_pattern_entries() + _staged_fragment_entries()
        assert _first_match_doc_id(entries, spoken_phrase) != "type-dictate-bypass"


# ============================================================================
# Consistency guard: embedded fixture TOML below must match the staged
# fragment file, so the two cannot silently drift apart.
# ============================================================================

def test_embedded_blocks_match_staged_fragment():
    staged = {e["doc_id"]: e for e in _staged_fragment_entries()}
    assert set(staged.keys()) == {"type-dictate-bypass", "enter", "tab"}

    assert staged["type-dictate-bypass"]["pattern"] == r"^(?:type|dictate) (.+)$"
    assert staged["type-dictate-bypass"]["actions"] == [
        {"function": "insert_text", "params": ["g1"]}
    ]

    # The fragment must not carry a literal-bypass block at all. The shipped
    # one stays byte-identical, and a staged copy would invite someone to
    # replace it. This assertion fails if the rejected widened block returns.
    assert "literal-bypass" not in staged, (
        "the shipped literal-bypass block must not be restaged or replaced"
    )

    assert staged["enter"]["pattern"] == r"^enter$"
    assert staged["enter"].get("whole_utterance_only") is True
    assert staged["enter"]["actions"] == [{"function": "press", "params": ["enter"]}]

    assert staged["tab"]["pattern"] == r"^tab$"
    assert staged["tab"].get("whole_utterance_only") is True
    assert staged["tab"]["actions"] == [{"function": "press", "params": ["tab"]}]


# ============================================================================
# Self-contained harness fixtures (do not touch the live patterns.toml)
# ============================================================================

# The SHIPPED literal-bypass block, byte-identical to
# services/wheelhouse/speech/config/patterns.toml line 65. This fragment
# does NOT change it, so every test that exercises "literal" is a
# regression test against unchanged behaviour.
_LITERAL_BYPASS_BLOCK = """
[[pattern]]
pattern = '''literal (.+)$'''
doc_id = "literal-bypass"
actions = [
    { function = "insert_text", params = ["g1"] }
]
"""

# The NEW block this fragment adds, placed immediately after the shipped
# literal-bypass block. It is ^-anchored, unlike literal-bypass, because
# "type" and "dictate" are ordinary English words that appear mid-sentence.
_TYPE_DICTATE_BLOCK = """
[[pattern]]
pattern = '''^(?:type|dictate) (.+)$'''
doc_id = "type-dictate-bypass"
actions = [
    { function = "insert_text", params = ["g1"] }
]
"""

# The two blocks in the order they will occupy in the live file. Every test
# for the new aliases uses this pair, not the new block alone, so the tests
# exercise the real first-match interaction between them.
_BYPASS_PAIR = (_LITERAL_BYPASS_BLOCK, _TYPE_DICTATE_BLOCK)

_TRAILING_SUBMIT_BLOCK = """
[[pattern]]
pattern = '''submit'''
doc_id = "submit-enter"
position = "trailing"
actions = [
    { function = "press_keys", params = ["enter"] }
]
"""

_BARE_ENTER_BLOCK = """
[[pattern]]
pattern = '''^enter$'''
doc_id = "enter"
whole_utterance_only = true
actions = [
    { function = "press", params = ["enter"] }
]
"""

_BARE_TAB_BLOCK = """
[[pattern]]
pattern = '''^tab$'''
doc_id = "tab"
whole_utterance_only = true
actions = [
    { function = "press", params = ["tab"] }
]
"""


def _write_patterns(tmp_path, *blocks, name="patterns.toml"):
    p = tmp_path / name
    p.write_text(_TOML_HEADER + "".join(blocks), encoding="utf-8")
    return str(p)


async def _harness(tmp_path, *blocks):
    path = _write_patterns(tmp_path, *blocks)
    h = SpeechPipelineHarness(patterns_path=path)
    await h.start()
    return h


def _insert_text_values(outputs):
    """Text carried by every insertion-shaped output, in order.

    Mirrors TestTrailingCommandLiteralEscape in
    test_speech_processor_trailing_command.py: the insert_text action
    surfaces as a generic ``intelligent_insert_text`` or ``insert_text`` IPC
    call, so check both possible param keys instead of the action name.

    CAVEAT, verified empirically while writing this fragment: ordinary
    per-word dictation ALSO surfaces as ``intelligent_insert_text`` with an
    ``insertion_string`` param -- there is no action-name or param-shape
    difference between "a literal-bypass insert fired" and "this word was
    just dictated normally". This helper is only safe to assert an exact
    value against when nothing else in the same utterance could ALSO
    produce insertion-shaped output (e.g. test_type_alias_inserts_text,
    where the whole utterance collapses to one insert). Where other, normal
    dictation output could also be present, use len(get_outputs()) instead
    (see test_word_boundary_guard_protects_words_ending_in_type).
    """
    values = []
    for out in outputs:
        text = out.params.get("insertion_string") or out.params.get("text")
        if text is not None:
            values.append(text)
    return values


def _press_key_actions(outputs):
    return [out for out in outputs if out.action == "press_key_action"]


def _hotkey_actions(outputs):
    return [out for out in outputs if out.action == "hotkey_action"]


# ----------------------------------------------------------------------------
# "type X" / "dictate X" as new aliases of the literal bypass
# ----------------------------------------------------------------------------

class TestTypeAndDictateAliases:
    """BLOCK 1: '''^(?:type|dictate) (.+)$''', a NEW block placed after the
    unchanged shipped literal-bypass block.

    Every test here builds a self-contained harness from BOTH blocks in
    their live order, so the first-match interaction between them is
    exercised rather than assumed. See
    test_embedded_blocks_match_staged_fragment for the drift guard that
    ties this embedded text to the staged fragment file.
    """

    @pytest.mark.asyncio
    async def test_type_alias_inserts_text(self, tmp_path):
        h = await _harness(tmp_path, *_BYPASS_PAIR)
        await h.send_utterance(["type", "hello"])
        await asyncio.sleep(0.1)
        assert _insert_text_values(h.get_outputs()) == ["hello"]
        await h.stop()

    @pytest.mark.asyncio
    async def test_dictate_alias_inserts_text(self, tmp_path):
        h = await _harness(tmp_path, *_BYPASS_PAIR)
        await h.send_utterance(["dictate", "hello"])
        await asyncio.sleep(0.1)
        assert _insert_text_values(h.get_outputs()) == ["hello"]
        await h.stop()

    @pytest.mark.asyncio
    async def test_literal_still_types_mid_utterance_word(self, tmp_path):
        """DESIGN CONSTRAINT regression (David, 2026-08-17, restated in the
        bead's CAUTION): "she is literal minded" must still type "she is
        minded" -- the un-anchored shape is unchanged for the pre-existing
        "literal" trigger. Empirically verified against this exact fixture
        before writing this assertion.
        """
        h = await _harness(tmp_path, *_BYPASS_PAIR)
        await h.send_utterance(["she", "is", "literal", "minded"])
        await asyncio.sleep(0.1)
        assert h.get_dictation_texts() == ["she", "is", "minded"]
        await h.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words",
        (
            ["what", "type", "of", "coffee", "do", "you", "want"],
            ["i", "will", "type", "this", "later"],
            ["please", "dictate", "the", "address"],
            ["she", "studies", "the", "prototype", "design"],
        ),
    )
    async def test_ordinary_sentences_dictate_word_for_word(
        self, tmp_path, words,
    ):
        """The anchoring regression, driven through the real harness.

        The first version of this fragment replaced the shipped block with
        '''\\b(?:literal|type|dictate) (.+)$'''. Measured against that
        pattern, "what type of coffee do you want" produced the single word
        "what" followed by a literally-typed "of coffee do you want", and
        "i will type this later" and "please dictate the address" failed
        the same way. The \\b guard only prevents a match INSIDE a word such
        as "prototype"; it does nothing about an ordinary whole word later
        in a sentence. The ^ anchor on BLOCK 1 is what prevents all four.

        A bypass match CONSUMES the trigger word and collapses everything
        after it into one captured-group insert, so "type hello" (2 words)
        produces exactly 1 output. Ordinary dictation output carries the
        same insertion_string shape, so the COUNT of outputs, not their
        param shape, is what proves nothing was consumed.
        """
        h = await _harness(tmp_path, *_BYPASS_PAIR)
        await h.send_utterance(list(words))
        await asyncio.sleep(0.1)
        assert h.get_dictation_texts() == list(words)
        assert len(h.get_outputs()) == len(words), (
            f"a hijacked match would collapse two or more words into one "
            f"output; got {h.get_outputs()!r}"
        )
        await h.stop()

    @pytest.mark.asyncio
    async def test_literal_submit_escape_hatch_unaffected(self, tmp_path):
        """Regression against the pre-existing shipped behavior covered by
        services/wheelhouse/tests/test_speech_processor_trailing_command.py
        ::TestTrailingCommandLiteralEscape -- "literal submit" must still
        insert "submit" as text and must NOT press Enter, with this
        fragment's new type-dictate-bypass block also in place.

        The shipped literal-bypass block is unchanged by this fragment, so
        this is a check that the ADDED block does not disturb it.
        """
        h = await _harness(
            tmp_path, *_BYPASS_PAIR, _TRAILING_SUBMIT_BLOCK,
        )
        await h.send_utterance(["literal", "submit"])
        await asyncio.sleep(0.1)
        assert _insert_text_values(h.get_outputs()) == ["submit"]
        assert _hotkey_actions(h.get_outputs()) == [], (
            "the literal escape hatch must not press Enter"
        )
        await h.stop()


# ----------------------------------------------------------------------------
# Bare "enter" / bare "tab"
# ----------------------------------------------------------------------------

class TestBareEnterAndTab:
    """BLOCKS 2-3: whole_utterance_only single-key press commands. See the
    BARE ENTER / BARE TAB note in wh-voice-access-parity.1.9.toml for why
    whole_utterance_only is required here (without it, a leading
    ``^enter$``/``^tab$`` with no other pattern sharing that first word
    would immediately command-prefix-split-execute on the FIRST word of any
    longer utterance, per services/wheelhouse/speech/router.py
    _cannot_match_with_next_word).
    """

    @pytest.mark.asyncio
    async def test_bare_enter_presses_enter(self, tmp_path):
        h = await _harness(tmp_path, _BARE_ENTER_BLOCK)
        await h.send_word("enter", start_of_utterance=True, end_of_utterance=True)
        await h.send_utterance_end_marker(h._utterance_counter)
        await asyncio.sleep(0.1)
        presses = _press_key_actions(h.get_outputs())
        assert len(presses) == 1
        assert presses[0].params == {"key": "enter", "repeat": 1}
        await h.stop()

    @pytest.mark.asyncio
    async def test_enter_mid_sentence_dictates_normally(self, tmp_path):
        """Without whole_utterance_only this would command-prefix-split and
        fire Enter on the bare word "enter" before "the following code"
        ever arrived. It must not: this is ordinary dictation.
        """
        h = await _harness(tmp_path, _BARE_ENTER_BLOCK)
        await h.send_utterance(["enter", "the", "following", "code"])
        await asyncio.sleep(0.1)
        assert _press_key_actions(h.get_outputs()) == []
        dictated = " ".join(h.get_dictation_texts())
        assert dictated == "enter the following code"
        await h.stop()

    @pytest.mark.asyncio
    async def test_bare_tab_presses_tab(self, tmp_path):
        h = await _harness(tmp_path, _BARE_TAB_BLOCK)
        await h.send_word("tab", start_of_utterance=True, end_of_utterance=True)
        await h.send_utterance_end_marker(h._utterance_counter)
        await asyncio.sleep(0.1)
        presses = _press_key_actions(h.get_outputs())
        assert len(presses) == 1
        assert presses[0].params == {"key": "tab", "repeat": 1}
        await h.stop()

    @pytest.mark.asyncio
    async def test_tab_mid_sentence_dictates_normally(self, tmp_path):
        h = await _harness(tmp_path, _BARE_TAB_BLOCK)
        await h.send_utterance(["tab", "the", "form", "please"])
        await asyncio.sleep(0.1)
        assert _press_key_actions(h.get_outputs()) == []
        dictated = " ".join(h.get_dictation_texts())
        assert dictated == "tab the form please"
        await h.stop()

    @pytest.mark.asyncio
    async def test_whole_utterance_only_flag_is_set(self, tmp_path):
        """Direct metadata check, not just behavioral: both entries must
        carry whole_utterance_only=True in the loaded catalog, the flag
        that makes the two tests above pass.
        """
        h = await _harness(tmp_path, _BARE_ENTER_BLOCK, _BARE_TAB_BLOCK)
        by_pattern = {
            entry["raw_pattern"]: entry for entry in h.catalog.all_patterns
        }
        assert by_pattern["^enter$"]["whole_utterance_only"] is True
        assert by_pattern["^tab$"]["whole_utterance_only"] is True
        await h.stop()


# ----------------------------------------------------------------------------
# THE REQUIRED GUARD TEST: bare enter must not endanger trailing "submit"
# ----------------------------------------------------------------------------

class TestSubmitGuard:
    """Bead CAUTION, load bearing: the trailing-position "submit" command
    already presses Enter at the end of an utterance, and the comment above
    that entry in patterns.toml (lines 299-302) records that a leading
    PLUS trailing pair for the SAME WORD silently pre-empts the trailing
    intercept in single-word utterances -- that is why a leading "submit"
    entry was removed previously. BLOCK 2 here is named "enter", a
    DIFFERENT word from "submit", so the collision key
    ({"enter"} vs {"submit"}) never overlaps -- verified directly via
    PatternCatalog's own trailing/leading collision check (pattern_catalog.py
    lines 663-676) in test_no_leading_trailing_word_collision below, not
    just asserted behaviorally.
    """

    @pytest.mark.asyncio
    async def test_hello_world_submit_still_types_words_and_presses_enter(
        self, tmp_path,
    ):
        """The exact utterance named in the bead's CAUTION and ACCEPTANCE
        CRITERIA, with the bare-enter entry present alongside literal-bypass
        and the trailing submit-enter entry.
        """
        h = await _harness(
            tmp_path,
            *_BYPASS_PAIR, _TRAILING_SUBMIT_BLOCK, _BARE_ENTER_BLOCK,
        )
        await h.send_word("hello", start_of_utterance=True, end_of_utterance=False)
        await h.send_word("world", start_of_utterance=False, end_of_utterance=False)
        await h.send_word("submit", start_of_utterance=False, end_of_utterance=False)
        await h.send_utterance_end_marker(h._utterance_counter)
        await asyncio.sleep(0.1)

        assert h.get_dictation_texts() == ["hello", "world"], (
            "the prefix must still be dictated verbatim with the bare-enter "
            "entry present"
        )
        hotkeys = _hotkey_actions(h.get_outputs())
        assert len(hotkeys) == 1, (
            f"the trailing submit action must fire exactly once; got {hotkeys!r}"
        )
        assert hotkeys[0].params.get("keys") == ["enter"]
        assert hotkeys[0].params.get("repeat") == 1
        assert _press_key_actions(h.get_outputs()) == [], (
            "the bare-enter entry (a different word from 'submit') must "
            "not itself fire -- only the trailing hotkey_action path should"
        )
        await h.stop()

    @pytest.mark.asyncio
    async def test_lone_submit_still_fires_with_no_dictation(self, tmp_path):
        """Regression against test_speech_processor_trailing_command.py
        ::TestTrailingCommandSingleWord, with the bare-enter entry now also
        present in the catalog.
        """
        h = await _harness(
            tmp_path,
            *_BYPASS_PAIR, _TRAILING_SUBMIT_BLOCK, _BARE_ENTER_BLOCK,
        )
        await h.send_word("submit", start_of_utterance=True, end_of_utterance=False)
        await h.send_utterance_end_marker(h._utterance_counter)
        await asyncio.sleep(0.1)

        assert h.get_dictation_texts() == []
        hotkeys = _hotkey_actions(h.get_outputs())
        assert len(hotkeys) == 1
        assert hotkeys[0].params.get("keys") == ["enter"]
        await h.stop()

    @pytest.mark.asyncio
    async def test_no_leading_trailing_word_collision(self, tmp_path):
        """Direct code-level proof, not inference: run the SAME collision
        check PatternCatalog itself runs at load time (pattern_catalog.py
        lines 663-676, ``set(trailing_commands.keys()) & set(first_words.
        keys())``) against the merged catalog and assert it is empty.
        """
        h = await _harness(
            tmp_path,
            *_BYPASS_PAIR, _TRAILING_SUBMIT_BLOCK, _BARE_ENTER_BLOCK,
        )
        assert "submit" in h.catalog.trailing_commands
        assert "enter" in h.catalog.first_words
        collisions = set(h.catalog.trailing_commands.keys()) & set(
            h.catalog.first_words.keys()
        )
        assert collisions == set()
        await h.stop()
