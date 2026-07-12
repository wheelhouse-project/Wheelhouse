"""Unified greedy-timer helper tests (wh-greedy-helper-impl).

Closes:
- wh-greedy-first-word-race: FRESH_REPLACEMENT and FRESH_COMMAND branches in
  _decide_idle always applied the standard 700 ms buffer timer, even when the
  single buffered word already fullmatched a greedy pattern. With a streaming
  STT pause >700 ms between the first wrapper word (e.g. "parentheses") and
  its content, the timer fired early and the wrapper emitted empty.
- wh-greedy-hotword-replacement-gap: in HOTWORD_BUFFERING mode,
  _decide_buffering filtered match_for_routing to ptype="command". A greedy
  replacement (e.g. \\bparentheses(.*)$) was filtered out, so the greedy
  guard missed it and the short timer applied.

Both bugs share one root cause: greedy-timer probes were duplicated and
inconsistent across entry points. The fix is a single shared helper that
every entry point consults.
"""
import re
import sys
from pathlib import Path
from typing import cast

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.router import SpeechRouter
from speech.pattern_catalog import PatternCatalog
from speech.word_event import WordEvent
from speech.domain import ProcessingMode, Action


GREEDY_TIMEOUT_MS = 5000
COMMAND_TIMEOUT_MS = 700
REPLACEMENT_TIMEOUT_MS = 400


@pytest.fixture
def catalog():
    return PatternCatalog("speech/config/patterns.toml")


@pytest.fixture
def router(catalog):
    return SpeechRouter(catalog, hotword="x-ray")


# ============================================================================
# Test A: FRESH_REPLACEMENT single-word greedy match (wh-greedy-first-word-race)
# ============================================================================

class TestFreshReplacementGreedy:
    """The first word "parentheses" already fullmatches \\bparentheses(.*)$
    (the greedy capture matches empty). Buffering it must use the long greedy
    timer, not the standard 400 ms replacement timer, so a slow STT delivering
    the wrapped content >400 ms later does not race the buffer timer.
    """

    def test_parentheses_first_word_uses_greedy_timer(self, router):
        event = WordEvent("parentheses", start_of_utterance=True, end_of_utterance=False)
        decision = router.decide(
            event,
            ProcessingMode.IDLE,
            [],
            {},
            hotword_active=False,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.BUFFER
        assert decision.target_mode == ProcessingMode.REPLACEMENT_BUFFERING
        assert decision.timeout_ms == GREEDY_TIMEOUT_MS, (
            f"FRESH_REPLACEMENT with greedy single-word match must use the "
            f"greedy timer ({GREEDY_TIMEOUT_MS}), got {decision.timeout_ms}. "
            f"wh-greedy-first-word-race."
        )

    def test_non_greedy_first_word_keeps_standard_replacement_timer(self, router):
        """Counter-test: a non-greedy replacement first word ("question" is a
        prefix of "question mark") must keep the standard replacement timer.
        Without this, a regression that always returned greedy_timeout_ms
        would pass the previous test.
        """
        event = WordEvent("question", start_of_utterance=True, end_of_utterance=False)
        decision = router.decide(
            event,
            ProcessingMode.IDLE,
            [],
            {},
            hotword_active=False,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms != GREEDY_TIMEOUT_MS, (
            f"Non-greedy FRESH_REPLACEMENT must not bleed the greedy timer; "
            f"got {decision.timeout_ms}."
        )


# ============================================================================
# Test B: FRESH_COMMAND single-word greedy match (wh-greedy-first-word-race)
# ============================================================================
#
# patterns.toml has no greedy command pattern that single-word fullmatches
# without requiring the hotword (^find\s*(.*)$ requires hotword; everything
# else needs at least one literal continuation word). Inject a synthetic
# greedy command pattern into a derived catalog so this path is testable.

class _SyntheticGreedyCommandCatalog:
    """Wrap a real PatternCatalog and add one synthetic greedy command pattern
    for ``trigger`` (no hotword required). Delegates everything else.
    """

    def __init__(self, real_catalog: PatternCatalog, trigger: str = "snapshot"):
        self._real = real_catalog
        self._trigger = trigger.lower()
        compiled = re.compile(rf"^{trigger}\s*(.*)$", re.IGNORECASE)
        self._injected_entry = (
            compiled,
            "command",
            {
                "actions": [],
                "requires_hotword": False,
                "is_greedy": True,
            },
        )
        from speech.pattern_catalog import PatternType
        self._pattern_type = PatternType.COMMAND

    def get_matching_patterns(self, word: str):
        existing = list(self._real.get_matching_patterns(word))
        if word.lower() == self._trigger:
            existing = [self._injected_entry] + existing
        return existing

    def get_pattern_type(self, word: str):
        if word.lower() == self._trigger:
            return self._pattern_type
        return self._real.get_pattern_type(word)

    def get_all_patterns(self):
        return self._real.get_all_patterns()


class TestFreshCommandGreedy:
    def test_synthetic_greedy_command_first_word_uses_greedy_timer(self, catalog):
        synthetic = _SyntheticGreedyCommandCatalog(catalog, trigger="snapshot")
        # _SyntheticGreedyCommandCatalog is a duck-typed test stand-in (only the
        # three catalog methods SpeechRouter touches are implemented). Cast at
        # the boundary so the type checker accepts the call; the runtime
        # duck-type contract is enforced by the tests themselves.
        router = SpeechRouter(cast(PatternCatalog, synthetic), hotword="x-ray")

        event = WordEvent("snapshot", start_of_utterance=True, end_of_utterance=False)
        decision = router.decide(
            event,
            ProcessingMode.IDLE,
            [],
            {},
            hotword_active=False,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.BUFFER
        assert decision.target_mode == ProcessingMode.COMMAND_BUFFERING
        assert decision.timeout_ms == GREEDY_TIMEOUT_MS, (
            f"FRESH_COMMAND with greedy single-word match must use the "
            f"greedy timer ({GREEDY_TIMEOUT_MS}), got {decision.timeout_ms}. "
            f"wh-greedy-first-word-race."
        )


# ============================================================================
# Test C: HOTWORD_BUFFERING + greedy replacement (wh-greedy-hotword-replacement-gap)
# ============================================================================

# ============================================================================
# Test D: FRESH_REPLACEMENT prefix-of-greedy (wh-greedy-helper-impl follow-up)
# ============================================================================
#
# Two-word greedy replacements still raced after the first wh-greedy-helper-impl
# slice. "angle" alone does NOT fullmatch \bangle brackets(.*)$ (the literal
# "brackets" is required), but it IS a prefix of the pattern's literal word
# sequence and the user clearly intends to keep speaking. The helper must
# return the greedy timeout for both fullmatch AND prefix cases.


class TestFreshReplacementGreedyPrefix:
    def test_angle_alone_uses_greedy_timer(self, router):
        """``angle`` is a prefix of ``\\bangle brackets(.*)$``."""
        event = WordEvent("angle", start_of_utterance=True, end_of_utterance=False)
        decision = router.decide(
            event,
            ProcessingMode.IDLE,
            [],
            {},
            hotword_active=False,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.BUFFER
        assert decision.target_mode == ProcessingMode.REPLACEMENT_BUFFERING
        assert decision.timeout_ms == GREEDY_TIMEOUT_MS, (
            f"'angle' is a prefix of greedy '\\bangle brackets(.*)$' and must "
            f"request the greedy timer ({GREEDY_TIMEOUT_MS}), got "
            f"{decision.timeout_ms}. wh-greedy-helper-impl follow-up."
        )

    def test_single_alone_uses_greedy_timer(self, router):
        """``single`` is a prefix of ``\\bsingle quotes?(.*)$``."""
        event = WordEvent("single", start_of_utterance=True, end_of_utterance=False)
        decision = router.decide(
            event,
            ProcessingMode.IDLE,
            [],
            {},
            hotword_active=False,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.BUFFER
        assert decision.target_mode == ProcessingMode.REPLACEMENT_BUFFERING
        assert decision.timeout_ms == GREEDY_TIMEOUT_MS, (
            f"'single' is a prefix of greedy '\\bsingle quotes?(.*)$' and "
            f"must request the greedy timer ({GREEDY_TIMEOUT_MS}), got "
            f"{decision.timeout_ms}. wh-greedy-helper-impl follow-up."
        )


class _SyntheticNonGreedyFirstWordCatalog:
    """Wrap a real PatternCatalog and inject one NON-greedy replacement
    pattern indexed under a synthetic first word. Used to verify that a
    leading FRESH_REPLACEMENT word that is NOT a prefix of any greedy
    pattern keeps the standard timer.
    """

    def __init__(self, real_catalog: PatternCatalog, trigger: str = "anglefoo"):
        self._real = real_catalog
        self._trigger = trigger.lower()
        compiled = re.compile(rf"\b{re.escape(trigger)} bar\b", re.IGNORECASE)
        self._injected_entry = (
            compiled,
            "replacement",
            {
                "actions": [],
                "requires_hotword": False,
                "is_greedy": False,
            },
        )
        from speech.pattern_catalog import PatternType
        self._pattern_type = PatternType.REPLACEMENT

    def get_matching_patterns(self, word: str):
        existing = list(self._real.get_matching_patterns(word))
        if word.lower() == self._trigger:
            existing = [self._injected_entry] + existing
        return existing

    def get_pattern_type(self, word: str):
        if word.lower() == self._trigger:
            return self._pattern_type
        return self._real.get_pattern_type(word)

    def get_all_patterns(self):
        return self._real.get_all_patterns()


class TestFreshReplacementNonGreedyLookalike:
    def test_anglefoo_keeps_standard_timer(self, catalog):
        """``anglefoo`` is FRESH_REPLACEMENT (synthetic, non-greedy) and is
        NOT a prefix of any greedy pattern indexed under it. The helper must
        return None and the standard replacement timer must apply.
        """
        synthetic = _SyntheticNonGreedyFirstWordCatalog(catalog, trigger="anglefoo")
        # Duck-typed test stand-in; cast at the boundary.
        router = SpeechRouter(cast(PatternCatalog, synthetic), hotword="x-ray")

        event = WordEvent("anglefoo", start_of_utterance=True, end_of_utterance=False)
        decision = router.decide(
            event,
            ProcessingMode.IDLE,
            [],
            {},
            hotword_active=False,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.BUFFER
        assert decision.target_mode == ProcessingMode.REPLACEMENT_BUFFERING
        assert decision.timeout_ms == REPLACEMENT_TIMEOUT_MS, (
            f"'anglefoo' must NOT bleed the greedy timer; got "
            f"{decision.timeout_ms}. The helper must return None when buffer "
            f"is not a prefix of any greedy pattern."
        )


# ============================================================================
# Test E: Switch-to-replacement branch (Finding 2)
# ============================================================================
#
# Step 3 of _decide_buffering (lines around 296-307 in router.py) handles the
# case where a COMMAND_BUFFERING attempt fails AND the buffer can search-match
# a replacement. The slice's contract was "every entry point that adds a word
# to the buffer consults the helper". This is one such entry point, and it
# previously hard-coded replacement_timeout_ms. The fix consults the helper
# with ("replacement",) before building the switch decision.
#
# patterns.toml has no natural word that is both COMMAND and greedy-REPLACEMENT
# first-word, so we inject one via a synthetic catalog wrapper.


class _SyntheticCommandPlusGreedyReplacementCatalog:
    """Wrap a real PatternCatalog and inject TWO patterns under the same
    trigger word:

    * A non-greedy COMMAND pattern (``^trigger foo$``) so FRESH_COMMAND
      classifies the word as a command.
    * A greedy REPLACEMENT pattern (``\\btrigger (.*)$``) that can
      search-match once the command attempt fails.

    When the buffer arrives as ``[trigger, busted]``, ``busted`` does not
    continue the command (because ``foo`` is required), but
    ``_can_match_replacement`` succeeds via the injected greedy replacement.
    The switch decision should therefore use the greedy timer, not the
    standard replacement timer.
    """

    def __init__(self, real_catalog: PatternCatalog, trigger: str = "synct"):
        self._real = real_catalog
        self._trigger = trigger.lower()
        cmd_compiled = re.compile(rf"^{trigger} foo$", re.IGNORECASE)
        repl_compiled = re.compile(rf"\b{trigger} (.*)$", re.IGNORECASE)
        self._command_entry = (
            cmd_compiled,
            "command",
            {
                "actions": [],
                "requires_hotword": False,
                "is_greedy": False,
            },
        )
        self._replacement_entry = (
            repl_compiled,
            "replacement",
            {
                "actions": [],
                "requires_hotword": False,
                "is_greedy": True,
            },
        )
        from speech.pattern_catalog import PatternType
        # COMMAND wins precedence -- this is how the real catalog classifies
        # mixed-type first words (see PatternCatalog.get_pattern_type).
        self._pattern_type = PatternType.COMMAND

    def get_matching_patterns(self, word: str):
        existing = list(self._real.get_matching_patterns(word))
        if word.lower() == self._trigger:
            existing = [self._command_entry, self._replacement_entry] + existing
        return existing

    def get_pattern_type(self, word: str):
        if word.lower() == self._trigger:
            return self._pattern_type
        return self._real.get_pattern_type(word)

    def get_all_patterns(self):
        # Include the injected replacement in all_patterns so
        # match_complete's search through ALL replacement patterns also
        # finds it (the implementation uses get_all_patterns to add
        # mid-text replacement matches).
        return self._real.get_all_patterns()


class TestSwitchToReplacementGreedyTimer:
    def test_switch_to_greedy_replacement_uses_greedy_timer(self, catalog):
        """COMMAND_BUFFERING with buffer ``[trigger]`` then word ``busted``:
        the command ``^trigger foo$`` no longer matches and cannot continue,
        but the buffer ``trigger busted`` search-matches the greedy
        replacement ``\\btrigger (.*)$``. The switch decision must request
        the greedy timer.
        """
        synthetic = _SyntheticCommandPlusGreedyReplacementCatalog(catalog, trigger="synct")
        router = SpeechRouter(cast(PatternCatalog, synthetic), hotword="x-ray")

        # Simulate the state: previous word "synct" landed us in COMMAND_BUFFERING.
        # Now "busted" arrives.
        event = WordEvent("busted", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event,
            ProcessingMode.COMMAND_BUFFERING,
            ["synct"],
            hotword_active=False,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.BUFFER
        assert decision.target_mode == ProcessingMode.REPLACEMENT_BUFFERING
        assert decision.timeout_ms == GREEDY_TIMEOUT_MS, (
            f"Switch-to-replacement with greedy replacement available must "
            f"use the greedy timer ({GREEDY_TIMEOUT_MS}), got "
            f"{decision.timeout_ms}. Finding 2."
        )
        assert "greedy" in (decision.reason or "").lower()


# ============================================================================
# Test F: Type-safety guard against bare-str pattern_types (Finding 4)
# ============================================================================


class TestPatternTypesMustBeTuple:
    def test_bare_str_pattern_types_raises_type_error(self, router):
        """``pattern_types="command"`` would silently iterate
        character-by-character and the helper would never find a match.
        The runtime guard fails loud with TypeError so the misuse is
        impossible to miss.
        """
        with pytest.raises(TypeError, match="must be a tuple"):
            router._greedy_timeout_for_buffer(
                ["parentheses"], "command", False, GREEDY_TIMEOUT_MS
            )


# ============================================================================
# Test C (original): HOTWORD_BUFFERING + greedy replacement
# ============================================================================


class TestHotwordBufferingGreedyReplacement:
    """User says hotword "x-ray", then "parentheses", then "hello". After
    "hello", the buffer is ["parentheses", "hello"]. Match_for_routing was
    previously called with ptype="command", filtering out the greedy
    replacement \\bparentheses(.*)$. The greedy guard therefore missed and
    the short timer applied. The helper must probe BOTH command and
    replacement patterns in HOTWORD_BUFFERING mode.
    """

    def test_hotword_buffering_greedy_replacement_uses_greedy_timer(self, router):
        event = WordEvent("hello", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event,
            ProcessingMode.HOTWORD_BUFFERING,
            ["parentheses"],
            hotword_active=True,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == GREEDY_TIMEOUT_MS, (
            f"HOTWORD_BUFFERING with greedy replacement match must use the "
            f"greedy timer ({GREEDY_TIMEOUT_MS}), got {decision.timeout_ms}. "
            f"wh-greedy-hotword-replacement-gap."
        )
        assert "greedy" in (decision.reason or "").lower()

    def test_hotword_buffering_no_greedy_keeps_standard_timer(self, router):
        """A non-greedy buffering path in HOTWORD_BUFFERING mode must NOT use
        the greedy timer.
        """
        event = WordEvent("space", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event,
            ProcessingMode.HOTWORD_BUFFERING,
            ["back"],
            hotword_active=True,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        if decision.action == Action.BUFFER:
            assert decision.timeout_ms != GREEDY_TIMEOUT_MS

    def test_hotword_buffering_greedy_command_still_uses_greedy_timer(self, router):
        """Regression: the wh-greedy-buffer-race scenario (greedy command in
        HOTWORD_BUFFERING) must keep working. Buffer "hey Google" matches
        the greedy command ^hey Google.*$.
        """
        event = WordEvent("Google", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event,
            ProcessingMode.HOTWORD_BUFFERING,
            ["hey"],
            hotword_active=True,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == GREEDY_TIMEOUT_MS


# ============================================================================
# Test: prefix probe must respect requires_hotword (wh-greedy-review-codex.1)
# ============================================================================
#
# patterns.toml has hotword-required greedy commands like ^find\s*(.*)$ and
# ^activates? (.+)$. Before this fix, the prefix probe matched those patterns
# even on a non-hotword utterance, so saying "find" alone applied the 5000 ms
# greedy timer. Result: an ordinary word that happens to start a protected
# command waited 5 seconds before being dictated. The prefix probe must skip
# requires_hotword candidates when the hotword is not active.

class TestPrefixProbeRespectsHotwordRequirement:
    """No-hotword "find" / "activate" must not incur a greedy-timer dictation
    latency regression, even though patterns.toml has hotword-required greedy
    command patterns whose literal prefixes are "find" / "activate".

    wh-greedy-review-codex.1 originally required these fresh non-hotword words
    to BUFFER with the standard command timer rather than the 5 s greedy timer.
    wh-l4h.1.14 supersedes that outcome with immediate dictation: because the
    only command patterns for these words (^find\\s*(.*)$, ^activates? (.+)$)
    are requires_hotword=true, a fresh non-hotword first word can never match a
    command, so the IDLE path finalizes it as dictation at once -- zero wait.
    This preserves wh-greedy-review-codex.1's intent (no latency regression) and
    improves it from "standard command timer" to "no buffering wait at all".
    """

    def test_no_hotword_find_uses_standard_command_timer(self, router):
        event = WordEvent("find", start_of_utterance=True, end_of_utterance=False)
        decision = router.decide(
            event,
            ProcessingMode.IDLE,
            [],
            {},
            hotword_active=False,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.DICTATE, (
            "fresh non-hotword 'find' must finalize as dictation immediately "
            "(wh-l4h.1.14); ^find\\s*(.*)$ is requires_hotword=true, so with the "
            "hotword inactive the word can never match a command and must not "
            "buffer at all."
        )

    def test_no_hotword_activate_uses_standard_command_timer(self, router):
        """Same shape against ^activates? (.+)$ at patterns.toml:143-144.

        wh-l4h.1.14: requires_hotword=true means a fresh non-hotword 'activate'
        finalizes as dictation immediately rather than buffering.
        """
        event = WordEvent("activate", start_of_utterance=True, end_of_utterance=False)
        decision = router.decide(
            event,
            ProcessingMode.IDLE,
            [],
            {},
            hotword_active=False,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.DICTATE

    def test_hotword_buffering_find_uses_greedy_timer(self, router):
        """Counter-test: when the hotword IS active, "find test" buffering
        must still attract the greedy timer (the bug fix must not regress the
        hotword-on case).
        """
        event = WordEvent("test", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event,
            ProcessingMode.HOTWORD_BUFFERING,
            ["find"],
            hotword_active=True,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == GREEDY_TIMEOUT_MS, (
            f"HOTWORD_BUFFERING 'find test' with hotword_active must keep "
            f"the greedy timer; got {decision.timeout_ms}. The hotword "
            f"requires_hotword skip must only apply when hotword is inactive."
        )


class TestLiteralPrefixPrecompute:
    """wh-greedy-prefix-precompute: the literal prefix is computed once at
    catalog load time (pattern_transform) and stored in the pattern data
    dict, so the runtime prefix probe reads a pre-computed string instead
    of regex-parsing the pattern on the hot path. The consistency test
    below is the safety net the original deepseek finding asked for: any
    future greedy pattern whose load-time prefix diverges from the
    runtime extraction fails loudly here instead of silently mis-routing
    the greedy timer.
    """

    def test_transform_pattern_stores_literal_prefix_for_greedy(self):
        from speech.pattern_transform import transform_pattern

        _, meta = transform_pattern(r"^angle brackets(.*)$")
        assert meta.get("is_greedy") is True
        assert meta.get("literal_prefix") == "angle brackets"

    def test_transform_pattern_omits_prefix_for_non_greedy(self):
        from speech.pattern_transform import transform_pattern

        _, meta = transform_pattern(r"^undo$")
        assert "literal_prefix" not in meta

    def test_every_catalog_greedy_pattern_prefix_matches_runtime_extraction(
        self, catalog
    ):
        """Every greedy pattern loaded from patterns.toml carries a
        pre-computed literal_prefix equal to what the runtime extractor
        produces from the compiled pattern."""
        seen_greedy = 0
        for word in catalog.get_all_first_words():
            for compiled, _ptype, data in catalog.get_matching_patterns(word):
                if not (data and data.get("is_greedy", False)):
                    continue
                seen_greedy += 1
                assert "literal_prefix" in data, compiled.pattern
                assert data["literal_prefix"] == (
                    SpeechRouter._extract_literal_prefix(compiled.pattern)
                ), compiled.pattern
        assert seen_greedy > 0
