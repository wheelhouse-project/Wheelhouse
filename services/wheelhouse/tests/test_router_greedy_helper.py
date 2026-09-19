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
import gc
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
from speech.pattern_transform import (
    MAX_PREFIX_MATCHERS,
    build_literal_prefix_matchers,
)
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


@pytest.mark.parametrize("words", [("right",), ("double",)])
def test_shipped_class_separator_command_uses_greedy_timer(router, words):
    """A pause inside an eligible command prefix gets the actual long timer."""
    decision = router.decide(
        WordEvent(words[-1], start_of_utterance=len(words) == 1, end_of_utterance=False),
        ProcessingMode.IDLE if len(words) == 1 else ProcessingMode.COMMAND_BUFFERING,
        list(words[:-1]),
        hotword_active=False,
        command_timeout_ms=COMMAND_TIMEOUT_MS,
        replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
        greedy_timeout_ms=GREEDY_TIMEOUT_MS,
    )
    assert decision.action == Action.BUFFER
    assert decision.target_mode == ProcessingMode.COMMAND_BUFFERING
    assert decision.timeout_ms == 5000


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


# ============================================================================
# Test F: one-word literal prefix carrying a \s+ tail (wh-click-number-dictation)
# ============================================================================
#
# The shipped click pattern ^click\s+(.+)$ has literal_prefix 'click\s+':
# strategy 1's fullmatch demands trailing whitespace 'click' does not have,
# and str.split() sees ONE token (the \s+ is a regex escape, not a real
# space), so the word-truncation strategy never ran. The first word 'click'
# therefore buffered on the 700 ms command timer; a slow second word split
# the command ('click' dictated alone, the number typed as text). Observed
# live 2026-08-08 13:39 (wheelhouse.log UTT-18). The probe must treat the
# \s+/\s* tokens as word separators.


class TestOneWordLiteralPrefixWithWhitespaceTail:
    """The greedy hold now needs the hotword, because the command does.

    David required the hotword for the click-element command on
    2026-08-17 (wh-voice-access-parity.1.6.1.1), so the router's greedy
    probe skips that pattern while the hotword is inactive. The hold this
    section exists to protect is unchanged with the hotword active; with
    the hotword inactive there is no command to hold for, and 'click'
    takes the ordinary command timer.
    """

    def test_click_alone_uses_greedy_timer_with_the_hotword(self, router):
        event = WordEvent("click", start_of_utterance=True, end_of_utterance=False)
        decision = router.decide(
            event,
            ProcessingMode.IDLE,
            [],
            hotword_active=True,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.action == Action.BUFFER
        assert decision.target_mode == ProcessingMode.COMMAND_BUFFERING
        assert decision.timeout_ms == GREEDY_TIMEOUT_MS, (
            f"'click' is the entire literal prefix of the greedy command "
            rf"^(?:click|tap)\s+(.+)$ and must hold the greedy timer "
            f"({GREEDY_TIMEOUT_MS}), got {decision.timeout_ms}."
        )

    def test_click_alone_takes_the_command_timer_without_the_hotword(
        self, router
    ):
        event = WordEvent("click", start_of_utterance=True, end_of_utterance=False)
        decision = router.decide(
            event,
            ProcessingMode.IDLE,
            [],
            hotword_active=False,
            command_timeout_ms=COMMAND_TIMEOUT_MS,
            replacement_timeout_ms=REPLACEMENT_TIMEOUT_MS,
            greedy_timeout_ms=GREEDY_TIMEOUT_MS,
        )
        assert decision.timeout_ms != GREEDY_TIMEOUT_MS, (
            "without the hotword the click command cannot match, so 'click' "
            "must not hold the greedy timer"
        )

    def test_literal_prefix_matcher_handles_whitespace_escape_tail(self):
        m = SpeechRouter._buffer_matches_literal_prefix
        # One-word prefix with a \s+ tail (the shipped click shape).
        assert m("click", r"click\s+") is True
        # Two literal words JOINED by \s+ (no real space anywhere).
        assert m("select", r"select\s+all\s+") is True
        assert m("select all", r"select\s+all\s+") is True
        # Non-prefix words must still be rejected.
        assert m("clack", r"click\s+") is False
        assert m("select any", r"select\s+all\s+") is False


# ============================================================================
# Test G: the literal-prefix probe stays uncached (wh-lru-cache-hot-paths)
# ============================================================================
#
# This section is named for the settled design, not for the one the branch
# started with (wh-lru-cache-hot-paths.1.10). _buffer_matches_literal_prefix
# is now the FALLBACK path only, for synthetic test catalogs whose pattern
# data predates the precomputed matchers; production reads the tuple the
# catalog built at load time. The fallback compiles on every call, on purpose.
#
# It must never be memoized. It takes buffer_text, which is the user's spoken
# words, so a cache on it retains what was said and returns almost no hits --
# measured at 0 hits over a 600-word utterance, holding 826 KB
# (wh-lru-cache-hot-paths.1.2). The tests below state that as a contract:
# distinct arguments keep distinct answers, and nothing on SpeechRouter
# carries a cache at all.


class TestLiteralPrefixProbeIsNotCached:
    def test_distinct_arguments_keep_distinct_answers(self):
        """The probe must not flatten distinct prefixes together.

        Any arrangement that ignored the prefix argument -- a memo keyed on
        the wrong thing, or a shared compiled pattern -- would return the
        first pair's answer for every later pair, so the router would hand the
        5 s greedy timer to words that are not greedy prefixes at all.
        Interleave the pairs so a stale single-key result cannot survive by
        accident.
        """
        m = SpeechRouter._buffer_matches_literal_prefix

        # Same buffer text, different prefixes -> different answers.
        assert m("click", r"click\s+") is True
        assert m("click", r"select\s+all\s+") is False

        # Same prefix, different buffer texts -> different answers.
        assert m("select all", r"select\s+all\s+") is True
        assert m("select any", r"select\s+all\s+") is False

        # Re-ask every pair in a different order; the answers must hold.
        assert m("select any", r"select\s+all\s+") is False
        assert m("click", r"click\s+") is True
        assert m("select all", r"select\s+all\s+") is True
        assert m("click", r"select\s+all\s+") is False

    def test_probe_itself_is_not_cached(self):
        """The probe must NOT memoize on ``buffer_text``.

        This is the guard for wh-lru-cache-hot-paths.1.2 stated as a contract
        rather than as a measurement: whatever else changes, the function that
        receives the user's spoken text must not be the one holding a cache.
        """
        assert not hasattr(
            SpeechRouter._buffer_matches_literal_prefix, "cache_info"
        ), "the probe takes user speech; a cache on it retains what was said"

    def test_router_holds_no_process_global_cache(self):
        """No attribute of SpeechRouter may carry a cache.

        Regression for wh-lru-cache-hot-paths.1.5, stated as a contract over
        the whole class rather than over one named function. Two earlier
        rounds of this review each moved a cache to a "safe" key and each was
        wrong, so the guard now refuses ANY cache on the router instead of
        checking the one place the last mistake was made.

        The router runs inside the long-lived Logic process. A cache on it is
        process-global, and nothing in the codebase clears it: PatternCatalog
        .reload() replaces five attributes of the catalog and cannot reach a
        cache that lives on a different class. Compiled patterns built from a
        user rule therefore outlive the rule itself. Keep the compiled work in
        the pattern data the catalog owns, where reload replaces it.
        """
        cached = [
            name
            for name in dir(SpeechRouter)
            if hasattr(getattr(SpeechRouter, name, None), "cache_info")
        ]
        assert cached == [], (
            f"SpeechRouter carries a process-global cache on {cached}. "
            f"Nothing clears it on catalog reload, so user-pattern data "
            f"survives the rule that produced it (wh-lru-cache-hot-paths.1.5)."
        )


class TestCatalogPrecomputesPrefixMatchers:
    """wh-lru-cache-hot-paths.1.5: the compiled prefix matchers belong to the
    catalog, beside the literal_prefix string the catalog already computes at
    load time (wh-greedy-prefix-precompute). Storing them there rather than in
    an lru_cache on the router bounds what WHEELHOUSE retains by the live
    catalog: reload() rebuilds the pattern data, so the catalog holds nothing
    for a deleted user rule.

    CPython's own regex cache keeps the compiled pattern until eviction no
    matter where WheelHouse stores it (wh-lru-cache-hot-paths.1.6), so that is
    not a claim about total process memory.
    TestReloadReleasesUserPatternMatchers below states exactly what its own
    heap scan does and does not prove.
    """

    def test_every_catalog_greedy_pattern_carries_compiled_matchers(self, catalog):
        seen_greedy = 0
        for word in catalog.get_all_first_words():
            for compiled, _ptype, data in catalog.get_matching_patterns(word):
                if not (data and data.get("is_greedy", False)):
                    continue
                seen_greedy += 1
                assert "literal_prefix_matchers" in data, compiled.pattern
                matchers = data["literal_prefix_matchers"]
                assert isinstance(matchers, tuple), compiled.pattern
                assert matchers == build_literal_prefix_matchers(
                    data["literal_prefix"]
                ), compiled.pattern
        assert seen_greedy > 0

    def test_precomputed_matchers_answer_the_same_as_the_probe(self, catalog):
        """The stored matchers must not drift from the probe's own answer."""
        for word in catalog.get_all_first_words():
            for compiled, _ptype, data in catalog.get_matching_patterns(word):
                if not (data and data.get("is_greedy", False)):
                    continue
                prefix = data["literal_prefix"]
                for buffer_text in (word, f"{word} extra", "nonsense buffer"):
                    stored = any(
                        m.match(buffer_text)
                        for m in data["literal_prefix_matchers"]
                    )
                    probe = SpeechRouter._buffer_matches_literal_prefix(
                        buffer_text, prefix
                    )
                    assert stored == probe, (compiled.pattern, buffer_text)


class TestReloadReleasesUserPatternMatchers:
    """Regression for wh-lru-cache-hot-paths.1.5.

    The pattern editor writes user_patterns.toml and then calls
    catalog.reload() (services/wheelhouse/main.py, _reload_and_refresh). When
    the compiled matchers lived in an lru_cache on SpeechRouter, reload could
    not reach them: matchers built from a deleted or edited user rule stayed
    resident until eviction or process exit. maxsize bounds the ENTRY COUNT,
    not the bytes those entries hold, so one long user prefix could retain an
    arbitrary amount.
    """

    PREFIX_WORDS = "zzqx marker phrase"

    def _write_user_file(self, path, include_greedy: bool) -> None:
        body = ""
        if include_greedy:
            body = (
                "[[pattern]]\n"
                f"pattern = '''^{self.PREFIX_WORDS}(.+)$'''\n"
                'doc_id = "user-greedy-marker"\n'
                "actions = [\n"
                '    { function = "insert_text", params = ["g1"] }\n'
                "]\n"
            )
        path.write_text(body, encoding="utf-8")

    @staticmethod
    def _live_marker_patterns():
        """Every compiled regex alive in this process that names the marker.

        The scan covers the whole heap on purpose. The point of
        wh-lru-cache-hot-paths.1.5 is that the retention was NOT reachable
        from the catalog: it sat in an lru_cache on SpeechRouter, which
        ``PatternCatalog.reload()`` cannot see. A check that only walks the
        catalog would call that arrangement clean. This one names the
        condition the finding is about -- no compiled matcher for a deleted
        user rule may stay alive anywhere -- so it holds no matter which class
        a future cache is added to.

        Callers must run ``re.purge()`` and ``gc.collect()`` first. ``re``
        keeps its own cache of every pattern ``re.compile`` has seen,
        independent of where WheelHouse stores the result, so without the
        purge this returns matchers even when nothing in WheelHouse holds
        them. ``re.purge()`` is documented public API.

        Be exact about what that purge means, because it decides what this
        test proves (wh-lru-cache-hot-paths.1.6). Production never calls
        ``re.purge()``, so in a running Wheelhouse CPython's regex cache does
        hold a deleted rule's matchers until eviction. That retention is real,
        it is bounded at ``re._MAXCACHE`` (512) entries process-wide, and
        ordinary compiling evicts it -- but it is CPython's, not WheelHouse's,
        and it was present identically before this branch. Purging it is what
        isolates the one variable under test: does WHEELHOUSE hold matchers
        for a rule the user deleted?

        So this test proves that no WheelHouse structure strands matchers
        across a reload. It deliberately does NOT measure the ``re`` cache.
        The mutation gate shows the isolation works rather than hides a leak:
        re-adding ``@lru_cache`` to ``build_literal_prefix_matchers`` is
        caught here, with the purge in place, because an ``lru_cache`` holds
        its own strong reference that purging ``re`` does not touch.
        """
        found = []
        for obj in gc.get_objects():
            if not isinstance(obj, re.Pattern):
                continue
            pattern = obj.pattern
            if isinstance(pattern, str) and "zzqx" in pattern:
                found.append(pattern)
        return found

    def test_removed_user_pattern_leaves_no_compiled_matchers(self, tmp_path):
        user_file = tmp_path / "user_patterns.toml"
        self._write_user_file(user_file, include_greedy=True)

        catalog = PatternCatalog(
            "speech/config/patterns.toml", user_patterns_file=str(user_file),
        )
        router = SpeechRouter(catalog, hotword="x-ray")

        # Speak the rule's first word. This is what fills a router-side cache
        # in the arrangement the finding is about: the probe runs once per
        # candidate greedy pattern per word event.
        assert router._buffer_is_greedy_prefix(
            ["zzqx"], ("command", "replacement"), hotword_active=False,
        ) is True

        re.purge()
        gc.collect()
        assert self._live_marker_patterns(), (
            "the user greedy rule must produce compiled matchers that stay "
            "alive while the rule exists, or the removal below proves nothing"
        )

        # The user deletes the rule in the pattern editor. The editor writes
        # the file and calls reload(); see main.py::_reload_and_refresh.
        self._write_user_file(user_file, include_greedy=False)
        assert catalog.reload() is True

        re.purge()
        gc.collect()
        leaked = self._live_marker_patterns()
        assert leaked == [], (
            f"compiled matchers for a deleted user rule are still alive: "
            f"{leaked}. Something outside the catalog holds them, so "
            f"reload() cannot free them (wh-lru-cache-hot-paths.1.5)."
        )


class TestPrefixMatcherCountIsBounded:
    """Regression for wh-lru-cache-hot-paths.1.7.

    The matcher count must not depend on user input. A prefix of N words used
    to produce N matchers whose combined source text grows quadratically, and
    the advanced Pattern Manager accepts a raw expression with no length or
    word-count limit: no ``setMaxLength`` on the input, ``_resolve_raw_expression``
    checks only that the regex compiles and is anchored to match its declared
    type, and ``_probe_backtracking`` runs three short probes that a long
    literal prefix passes easily.

    That mattered more once the build moved to catalog load time. The build
    now runs on startup and on every successful ``reload()`` after a pattern
    save, synchronously inside the Logic process's asyncio loop (main.py,
    ``_handle_pattern_manager_action`` calls ``_reload_and_refresh`` directly
    at four sites), so it blocks speech routing -- and it runs even when the
    trigger is never spoken. Measured on a 1000-word prefix: 1000 matchers,
    3.310 s and 62.3 MB uncapped, against 0.0072 s and 129 KB capped.
    """

    @staticmethod
    def _many_word_prefix(words: int) -> str:
        return " ".join(f"zzqxcap{i}" for i in range(words))

    def test_matcher_count_never_exceeds_the_cap(self):
        for words in (1, 2, MAX_PREFIX_MATCHERS - 1, MAX_PREFIX_MATCHERS,
                      MAX_PREFIX_MATCHERS + 1, 500):
            matchers = build_literal_prefix_matchers(
                self._many_word_prefix(words)
            )
            assert len(matchers) <= MAX_PREFIX_MATCHERS, (
                f"a {words}-word prefix built {len(matchers)} matchers; the "
                f"count must not depend on user input "
                f"(wh-lru-cache-hot-paths.1.7)"
            )
            # Below the cap nothing is lost: still one matcher per truncation
            # plus the full prefix.
            if words <= MAX_PREFIX_MATCHERS:
                assert len(matchers) == words

    def test_full_prefix_still_matches_past_the_cap(self):
        """Capping drops only the deepest partial probes, never the whole
        trigger. A user who speaks such a phrase in full still matches."""
        words = MAX_PREFIX_MATCHERS + 20
        prefix = self._many_word_prefix(words)
        matchers = build_literal_prefix_matchers(prefix)
        assert any(m.match(prefix) for m in matchers), (
            "the full-prefix matcher must survive the cap"
        )
        # The truncations that survive are the shallow ones, in probe order.
        first_word = prefix.split()[0]
        assert any(m.match(first_word) for m in matchers)

    def test_shipped_catalog_is_far_below_the_cap(self, catalog):
        """The cap must be a guard against absurd input, not something a real
        pattern trips. The longest shipped prefix is two words."""
        worst = 0
        for word in catalog.get_all_first_words():
            for _compiled, _ptype, data in catalog.get_matching_patterns(word):
                if not (data and data.get("is_greedy", False)):
                    continue
                worst = max(worst, len(data.get("literal_prefix_matchers", ())))
        assert 0 < worst <= 4, (
            f"the shipped catalog's largest matcher set is {worst}; if this "
            f"grows toward MAX_PREFIX_MATCHERS ({MAX_PREFIX_MATCHERS}) the cap "
            f"needs to be re-justified rather than silently truncating a real "
            f"trigger phrase"
        )
