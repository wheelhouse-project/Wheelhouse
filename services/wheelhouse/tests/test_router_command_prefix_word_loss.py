"""wh-cmd-prefix-word-loss: a command word spoken INSIDE a sentence must
dictate, not execute and swallow the rest of the utterance.

David reported on 2026-08-26 that dictating "redo this prompt" pressed
Ctrl+Y and typed only "this prompt". Two router paths produce that class of
loss, and both already consult ``whole_utterance_only``:

* ``_decide_idle`` (router.py, "Single word command complete") fires a
  one-word command the instant it arrives, before the rest of the utterance
  is spoken. That is what made bare "escape" swallow the sentence it began.
* ``_resolve_finalization`` step 1b, the command-PREFIX split added by
  wh-cmd-prefix-not-split, tries every prefix length longest-first, so any
  leading command phrase executes and the remaining words dictate.

The fix is data, not code: the exposed command patterns in
``speech/config/patterns.toml`` carry ``whole_utterance_only = true``, which
both paths already honour. The accepted cost (David, 2026-08-26) is that a
bare no-continuation command now waits for the utterance-ending pause
instead of firing instantly.

The split itself is NOT a mistake -- "backspace hello world" is the shape it
was built for. Three patterns therefore keep it; see ``_EXCLUSIONS`` below.
"""
import re
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.domain import Action, ProcessingMode
from speech.pattern_catalog import PatternCatalog
from speech.router import SpeechRouter
from speech.word_event import WordEvent


@pytest.fixture(scope="module")
def catalog():
    # user_patterns_file="" mirrors tests/e2e/conftest.py: the session-scoped
    # autouse guard in tests/conftest.py already forces the default
    # resolution to "", and this is the second line of defense that keeps
    # the developer's personal data/user_patterns.toml out of the catalog.
    return PatternCatalog("speech/config/patterns.toml", user_patterns_file="")


@pytest.fixture
def router(catalog):
    return SpeechRouter(catalog, hotword="x-ray")


# ---------------------------------------------------------------------------
# Criterion 1 -- a command phrase that merely BEGINS an utterance dictates
# ---------------------------------------------------------------------------

class TestCommandInsideSentenceDictates:
    @pytest.mark.parametrize(
        "phrase",
        [
            # The reported repro. Before the fix: EXECUTE "redo",
            # remainder "this prompt".
            "redo this prompt",
            # Two-word leading command: EXECUTE "copy that".
            "copy that idea into the spec",
            # Three-word leading command: EXECUTE "delete this paragraph".
            "delete this paragraph and rewrite it",
            # Four- and five-word leading commands. The prefix loop has no
            # word-count bound, so length alone never protected an
            # utterance (dispatcher ruling 1, 2026-08-26).
            "push to talk mode is confusing",
            "snap window to the left and keep typing",
            "delete next three words of the sentence",
            "show all windows on my second screen",
        ],
    )
    def test_leading_command_phrase_dictates_every_word(self, router, phrase):
        decision = router._resolve_finalization(
            phrase.split(), hotword_active=False
        )
        assert decision.action is Action.DICTATE, (
            f"{phrase!r} executed instead of dictating: "
            f"payload={decision.payload!r} remainder={decision.remainder!r}"
        )
        assert decision.payload == phrase

    def test_escape_inside_a_sentence_no_longer_fires(self, router):
        """"escape characters need quotes" -- criterion 1, third phrase.

        Before the fix this executed the ``^escape$`` command and dropped
        "escape" from the text; it also fired on the STREAMING fast path,
        because ^escape$ is one word that cannot continue into a longer
        command, so it went before "characters need quotes" was spoken.

        It does not finalize as a plain DICTATE, for a reason that predates
        this bead and is independent of it: the trailing word "quotes" is a
        WRAPPING replacement whose trigger matches the bare word anywhere
        in an utterance, which patterns.toml documents as load-bearing
        ordering in the Voice Access punctuation section. The router
        therefore finalizes the utterance on the replacement path and
        carries the words before it in ``before_remainder``.

        What this bead owns is the assertion below: the leading command
        word survives as text instead of being executed and swallowed.
        """
        decision = router._resolve_finalization(
            "escape characters need quotes".split(), hotword_active=False
        )
        assert decision.before_remainder == "escape characters need", (
            "the ^escape$ command still consumed the start of the "
            f"utterance: {decision!r}"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "NOT fixable by whole_utterance_only. '^go (.+)' is a GREEDY "
            "no-hotword command (cursor_navigate 'go g1'), so this utterance "
            "is claimed by the WHOLE-buffer match at _resolve_finalization "
            "step 1, never by the prefix split at step 1b. The flag governs "
            "the early-execute paths only -- a whole-utterance match is "
            "exactly what it permits -- and greedy patterns are outside the "
            "eligibility rule of wh-cmd-prefix-word-loss. Flagging the "
            "non-greedy '^go to (?:the )?end of (?:the )?line$' does not "
            "help, because the greedy pattern matches first. Strict on "
            "purpose: whoever narrows '^go (.+)' must delete this marker."
        ),
    )
    def test_go_to_end_of_line_breaks_and_fix_them_dictates(self, router):
        phrase = "go to end of line breaks and fix them"
        decision = router._resolve_finalization(
            phrase.split(), hotword_active=False
        )
        assert decision.action is Action.DICTATE
        assert decision.payload == phrase

    def test_no_command_fires_on_the_first_word(self, router):
        """The streaming fast path must not execute a bare command word.

        ``^escape$`` is one word and cannot continue into a longer command,
        so ``_decide_idle`` used to return EXECUTE the moment it arrived --
        before "characters need quotes" was even spoken. Buffering instead
        is what lets the finalization path see the whole utterance.
        """
        decision = router.decide(
            WordEvent("escape", start_of_utterance=True, end_of_utterance=False),
            ProcessingMode.IDLE,
            [],
        )
        assert decision.action is not Action.EXECUTE, (
            f"'escape' executed on arrival: reason={decision.reason!r}"
        )


# ---------------------------------------------------------------------------
# Criterion 2 -- a bare command is still a command
# ---------------------------------------------------------------------------

class TestBareCommandStillExecutes:
    @pytest.mark.parametrize(
        "phrase",
        [
            "redo",
            "redo that",
            "undo 3",
            "escape",
            "select all",
            "delete next three words",
            "go to end of line",
        ],
    )
    def test_whole_utterance_command_executes(self, router, phrase):
        decision = router._resolve_finalization(
            phrase.split(), hotword_active=False
        )
        assert decision.action is Action.EXECUTE, (
            f"{phrase!r} stopped executing: payload={decision.payload!r}"
        )
        assert decision.payload == phrase
        assert not decision.remainder


# ---------------------------------------------------------------------------
# Criteria 3 and 5 -- the four patterns that keep the prefix split
# ---------------------------------------------------------------------------

# Every entry here is a command pattern that CAN fire on a proper leading
# prefix and deliberately still does. They are named here, in code, so a
# later reader cannot mistake them for patterns the sweep overlooked.
#
# ^back ?space\s*(\d+)?$ is the motivating case of the prefix-split feature
# itself (wh-cmd-prefix-not-split): "backspace hello world" is what step 1b
# was built to handle, and taking the split away would restore the bug that
# change fixed. "Backspace" is an ordinary English word too, so the split
# does cost a sentence that opens with it. That cost is the decided
# contract of wh-cmd-prefix-not-split, and only that contract keeps this
# pattern here.
#
# ^undu\s*(\d+)?$ and ^redu\s*(\d+)?$ were here until 2026-09-03 and are
# deliberately NOT here now (wh-number-words-one-parser.1.27). The reason
# recorded above -- neither is an English word, so no sentence begins with
# either -- was true and is still true. What changed is that the split cost
# nothing only while a count was one word. Multi-word counts arrived on this
# branch, and the split then severed them: "undu twenty three" fired the
# prefix "undu" with the count "twenty", pressed Ctrl+Z twenty times, and
# typed "three" into the document. The flag costs the mishearing nothing,
# exactly as it costs "undue" nothing, because a bare "undu" and "undu 3"
# still execute as whole utterances. See TestTheMisheardAliasesWaitForTheCount
# below. Ruled by David 2026-09-03 (QUESTIONS-2026-09-02.md item 6).
#
# ^undue\s*(\d+)?$ was a fourth entry until 2026-08-26 and is deliberately
# NOT here (wh-cmd-prefix-word-loss.1.1, filed by deepseek in round 1 and
# ruled 2026-08-26). "Undue" IS an ordinary English adjective, so the
# exclusion kept this bead's own defect alive: "undue influence" executed
# the undo, fired Ctrl+Z and typed only "influence". No
# wh-cmd-prefix-not-split contract covers it, and the flag costs the
# mishearing nothing, because bare "undue" and "undue 3" still execute as
# whole utterances. See TestUndueIsAnEnglishWord below.
#
# ^undo(?: that)?\s*(\d+)?$ and ^redo(?: that)?\s*(\d+)?$ are NOT here:
# criterion 1 requires "redo this prompt" to dictate every word.
_EXCLUSIONS = {
    r"^back ?space\s*(\d+)?$",
}


class TestBackspaceClassKeepsItsSplit:
    @pytest.mark.parametrize(
        "phrase,command",
        [
            ("backspace hello world", "backspace"),
            ("back space hello world", "back space"),
        ],
    )
    def test_excluded_command_still_splits(self, router, phrase, command):
        decision = router._resolve_finalization(
            phrase.split(), hotword_active=False
        )
        assert decision.action is Action.EXECUTE
        assert decision.payload == command
        assert decision.remainder == "hello world"

    def test_backspace_keeps_its_count(self, router):
        # wh-cmd-prefix-not-split guarded this; the exclusion keeps it true.
        decision = router._resolve_finalization(
            ["back", "space", "3", "hello"], hotword_active=False
        )
        assert decision.action is Action.EXECUTE
        assert decision.payload == "back space 3"
        assert decision.remainder == "hello"


class TestTheMisheardAliasesWaitForTheCount:
    """wh-number-words-one-parser.1.27: "undu" and "redu" left _EXCLUSIONS.

    They are mishearing spellings, so nobody starts a sentence with
    either, and that is why commit 364c1d96 let them fire on their first
    word. Multi-word counts made the split cost something anyway: the
    command fired with the first word of the count and the rest was
    typed. These aliases now behave exactly as "undo" and "redo" do.

    The pair of assertions matters as much as either one alone. The first
    is the change; the second is what the change must not cost, since an
    alias that stopped firing at all would take the mishearing recovery
    with it.
    """

    @pytest.mark.parametrize(
        "phrase,pressed",
        [
            ("undu twenty three", 23),
            ("redu twenty three", 23),
        ],
    )
    def test_a_two_word_count_is_not_severed(self, router, phrase, pressed):
        decision = router._resolve_finalization(
            phrase.split(), hotword_active=False
        )
        assert decision.action is Action.EXECUTE, (
            f"{phrase!r} did not execute: payload={decision.payload!r}"
        )
        assert decision.payload == phrase
        assert not decision.remainder, (
            f"{phrase!r} left {decision.remainder!r} to be typed into the "
            f"document; the whole count belongs to the command"
        )

    @pytest.mark.parametrize(
        "phrase", ["undu hello world", "redu hello world"]
    )
    def test_the_alias_inside_a_sentence_dictates(self, router, phrase):
        decision = router._resolve_finalization(
            phrase.split(), hotword_active=False
        )
        assert decision.action is Action.DICTATE, (
            f"{phrase!r} executed instead of dictating: "
            f"payload={decision.payload!r} remainder={decision.remainder!r}"
        )
        assert decision.payload == phrase

    @pytest.mark.parametrize(
        "phrase", ["undu", "undu 3", "redu", "redu 2"]
    )
    def test_the_alias_alone_still_executes(self, router, phrase):
        """The mishearing recovery, which the flag must not cost."""
        decision = router._resolve_finalization(
            phrase.split(), hotword_active=False
        )
        assert decision.action is Action.EXECUTE
        assert decision.payload == phrase
        assert not decision.remainder


class TestUndueIsAnEnglishWord:
    """wh-cmd-prefix-word-loss.1.1: "undue" left the exclusion set.

    The alias exists because speech recognition mishears "undo" as
    "undue", and its action fires ctrl+z. While it kept the prefix split,
    every sentence opening with the English adjective lost its first word:
    "undue influence" executed the undo and typed only "influence". That
    is the same defect this bead exists to remove, still reachable by a
    word people say. Flagging it costs the mishearing nothing, because the
    whole-buffer match at step 1 still fires for a bare "undue".
    """

    @pytest.mark.parametrize(
        "phrase",
        [
            "undue influence",
            "undue burden",
            "undue haste makes waste",
            "undue pressure on the team",
        ],
    )
    def test_undue_inside_a_sentence_dictates(self, router, phrase):
        decision = router._resolve_finalization(
            phrase.split(), hotword_active=False
        )
        assert decision.action is Action.DICTATE, (
            f"{phrase!r} executed instead of dictating: "
            f"payload={decision.payload!r} remainder={decision.remainder!r}"
        )
        assert decision.payload == phrase

    @pytest.mark.parametrize("phrase", ["undue", "undue 3"])
    def test_undue_alone_still_executes(self, router, phrase):
        """The mishearing recovery, which the flag must not cost."""
        decision = router._resolve_finalization(
            phrase.split(), hotword_active=False
        )
        assert decision.action is Action.EXECUTE
        assert decision.payload == phrase
        assert not decision.remainder


# ---------------------------------------------------------------------------
# Criterion 4 -- catalog-wide coverage
# ---------------------------------------------------------------------------

try:  # Python 3.11+ moved the regex parser; sre_parse is a deprecated shim.
    from re import _parser as _regex_parser
except ImportError:  # pragma: no cover - older interpreters
    import sre_parse as _regex_parser


def _shortest_utterance(compiled: re.Pattern) -> str | None:
    """Return the shortest string the COMPILED pattern fullmatches.

    Reading the compiled pattern is the point, not a convenience: the
    catalog rewrites ``(\\d+)`` to ``(\\w+)`` at load, so the raw text
    ``^item (\\d+)$`` is really ``^item (\\w+)$`` at runtime and fires on
    ANY two-word phrase, not only on a number. A sweep that read
    ``raw_pattern`` would score that entry as safe.

    Every alternation contributes its shortest branch and every optional
    group contributes zero repeats. Returns None when the parse hits a
    construct this walker does not model, which the caller treats as a
    failure rather than a skip.
    """

    def walk(node) -> str | None:
        out = []
        for opcode, argument in node:
            name = str(opcode)
            if name == "LITERAL":
                out.append(chr(argument))
            elif name in ("AT", "ASSERT", "ASSERT_NOT"):
                continue  # anchors and lookarounds add no characters
            elif name in ("ANY", "NOT_LITERAL"):
                out.append("x")
            elif name == "IN":
                out.append(_shortest_member(argument))
            elif name in ("MAX_REPEAT", "MIN_REPEAT"):
                minimum, _maximum, subpattern = argument
                piece = walk(subpattern)
                if piece is None:
                    return None
                out.append(piece * minimum)
            elif name == "SUBPATTERN":
                piece = walk(argument[3])
                if piece is None:
                    return None
                out.append(piece)
            elif name == "BRANCH":
                shortest = None
                for branch in argument[1]:
                    piece = walk(branch)
                    if piece is None:
                        continue
                    if shortest is None or len(piece) < len(shortest):
                        shortest = piece
                if shortest is None:
                    return None
                out.append(shortest)
            else:
                return None
        return "".join(out)

    def _shortest_member(members) -> str:
        """One character from a character class such as ``\\s`` or ``[a-z]``.

        ``\\s`` must yield a SPACE, not a letter: the counted commands are
        written ``^delete next\\s+(\\d+)?\\s*words?$``, and a letter there
        produces "delete nextxwords", which fullmatches nothing.
        """
        if any(str(kind) == "NEGATE" for kind, _value in members):
            return "x"
        for kind, value in members:
            kind = str(kind)
            if kind == "LITERAL":
                return chr(value)
            if kind == "RANGE":
                return chr(value[0])
            if kind == "CATEGORY":
                category = str(value)
                if "DIGIT" in category:
                    return "7"
                if "SPACE" in category:
                    return " "
                return "x"
        return "x"

    try:
        generated = walk(_regex_parser.parse(compiled.pattern, re.IGNORECASE))
    except Exception:  # noqa: BLE001 - an unparsable pattern must fail loudly
        return None
    if generated is None:
        return None
    # The router rebuilds a candidate from buffer[:k] -- a whitespace-joined
    # sequence of WORDS -- so a match only describes a reachable utterance
    # once its whitespace is normalised the same way.
    utterance = " ".join(generated.split())
    return utterance if utterance and compiled.fullmatch(utterance) else None


def _eligible_patterns(catalog):
    """Command patterns the prefix split can reach without the hotword.

    Greedy patterns are excluded because a greedy command consumes the rest
    of the buffer, so the prefix loop already skips them. Hotword-gated
    patterns are excluded because the hotword marks command context: a
    split there is deliberate (accepted residual, wh-cmd-prefix-word-loss).
    """
    return [
        row
        for row in catalog.get_all_patterns()
        if row["pattern_type"] == "command"
        and not row.get("is_user")
        and not row.get("is_greedy")
        and not row.get("requires_hotword")
    ]


class TestEveryExposedCommandIsWholeUtteranceOnly:
    def test_catalog_holds_no_user_patterns(self, catalog):
        """Hermeticity: user_patterns.toml is per-machine and out of scope."""
        assert not [r for r in catalog.get_all_patterns() if r.get("is_user")]

    def test_every_eligible_pattern_yields_an_utterance(self, catalog):
        """No pattern may be silently skipped by the walker.

        If this fails, the coverage test below is scoring fewer patterns
        than the catalog holds and its pass means nothing.
        """
        unparsed = [
            row["raw_pattern"]
            for row in _eligible_patterns(catalog)
            if _shortest_utterance(row["compiled_pattern"]) is None
        ]
        assert not unparsed, (
            f"{len(unparsed)} eligible patterns produced no utterance: "
            f"{unparsed}"
        )

    def test_no_eligible_pattern_severs_dictation(self, catalog, router):
        """The behavioural form of the rule, driven through the router.

        For every eligible pattern, take the shortest utterance it matches,
        append two ordinary dictation words, and require that the router
        does not execute the leading phrase and sever the rest. A severed
        remainder IS the defect: those words were spoken and never typed.

        A whole-utterance greedy match (payload is the entire buffer,
        remainder empty) is a different shape and out of scope here.
        """
        severed = []
        for row in _eligible_patterns(catalog):
            if row["raw_pattern"] in _EXCLUSIONS:
                continue
            utterance = _shortest_utterance(row["compiled_pattern"])
            assert utterance is not None, row["raw_pattern"]
            decision = router._resolve_finalization(
                utterance.split() + ["hello", "world"], hotword_active=False
            )
            if decision.action is Action.EXECUTE and decision.remainder:
                severed.append(
                    f"{row['raw_pattern']} -> executed "
                    f"{decision.payload!r}, dropped {decision.remainder!r}"
                )
        assert not severed, (
            f"{len(severed)} command patterns still swallow dictation:\n"
            + "\n".join(severed)
        )

    def test_no_eligible_pattern_lacks_the_flag(self, catalog):
        """The structural form of the rule, so a NEW pattern cannot slip in.

        A pattern added later without ``whole_utterance_only`` defaults to
        the split and reintroduces the defect. The behavioural test above
        can miss such a pattern when some other entry claims its utterance
        first; this one cannot.
        """
        unflagged = sorted(
            row["raw_pattern"]
            for row in _eligible_patterns(catalog)
            if not row.get("whole_utterance_only")
            and row["raw_pattern"] not in _EXCLUSIONS
        )
        assert not unflagged, (
            f"{len(unflagged)} command patterns lack whole_utterance_only. "
            "Add the flag, or add the pattern to _EXCLUSIONS with the "
            f"reason in a comment:\n" + "\n".join(unflagged)
        )

    def test_the_exclusions_are_present_and_unflagged(self, catalog):
        """The exclusion set must describe reality, not an old file.

        A renamed or deleted pattern would leave a dead entry in
        ``_EXCLUSIONS`` that silently exempts nothing.
        """
        eligible = {row["raw_pattern"]: row for row in _eligible_patterns(catalog)}
        for raw in sorted(_EXCLUSIONS):
            assert raw in eligible, f"{raw} is no longer an eligible pattern"
            assert not eligible[raw].get("whole_utterance_only"), (
                f"{raw} gained whole_utterance_only; it must keep its split "
                "or leave _EXCLUSIONS"
            )
# ---------------------------------------------------------------------------
# wh-voice-access-parity.1.6.2.1 -- the seven Voice Access alias entries,
# named one by one in both directions
# ---------------------------------------------------------------------------
#
# Codex filed .1.6.2.1 against the Voice Access wording branch: seven command
# entries lacked ``whole_utterance_only``, so "show numbers in the report"
# ran the command mid-sentence and dropped the rest of the words. The data
# fix landed with wh-cmd-prefix-word-loss in commit c3a54348, which flagged
# 153 patterns by a rule rather than by a hand list, and the two sweep tests
# in ``TestEveryExposedCommandIsWholeUtteranceOnly`` above already cover
# these seven generically.
#
# The sweeps are not enough for this finding, for two reasons. First, they
# prove only the mid-sentence direction; nothing sweeps the other way, so a
# change that flagged a pattern into silence would pass every one of them.
# Second, a sweep names no phrase, so a reader cannot see that the seven
# reported entries are the ones covered. The signed acceptance for
# wh-voice-access-parity.1.6 asks for both directions per phrase, by name.
#
# Every phrase below was measured against the real SpeechRouter before the
# class was written, so the expectations record observed behaviour rather
# than an assumption about it.

_SEVEN_ENTRIES_MID_SENTENCE = [
    # doc_id select-word: ^select (?:this )?word$
    "select word by word until it looks right",
    "select this word processor and open it",
    # doc_id select-line: ^select (?:this )?line$
    "select line six and copy it",
    "select this line item and total it",
    # doc_id select-paragraph: ^select (?:this )?paragraph$
    "select paragraph three of the contract",
    "select this paragraph and rewrite it",
    # doc_id show-numbers: ^(?:show|apply) numbers$ -- codex reported the
    # first of these two as a live mid-sentence execution.
    "show numbers in the report",
    "apply numbers to the invoice rows",
    # doc_id hide-numbers: ^(?:hide|dismiss) numbers$
    "hide numbers on the chart",
    "dismiss numbers from the summary",
    # doc_id show-grid: ^(?:show|apply) grid$
    "show grid lines on the chart",
    "apply grid spacing to the layout",
    # doc_id hide-grid: ^(?:hide|dismiss) grid$
    "hide grid lines before printing",
    "dismiss grid overlays for now",
]

_SEVEN_ENTRIES_WHOLE_UTTERANCE = [
    "select word",
    "select this word",
    "select line",
    "select this line",
    "select paragraph",
    "select this paragraph",
    "show numbers",
    "apply numbers",
    "hide numbers",
    "dismiss numbers",
    "show grid",
    "apply grid",
    "hide grid",
    "dismiss grid",
]


class TestVoiceAccessAliasEntriesKeepBothDirections:
    """Finding wh-voice-access-parity.1.6.2.1, both directions per phrase.

    Fourteen spoken forms cover the seven entries: each of the three select
    forms takes an optional "this", and each of the four overlay and grid
    forms has a Voice Access word and the older Wheelhouse word restored as
    an alias by wh-dismiss-alias-restore.
    """

    @pytest.mark.parametrize("phrase", _SEVEN_ENTRIES_MID_SENTENCE)
    def test_the_phrase_inside_a_longer_sentence_dictates(self, router, phrase):
        """Direction one: the words are typed, and none are lost.

        The defect codex reported is the remainder: the leading command
        executed and the words after it were dropped. Asserting DICTATE on
        the whole phrase covers both halves of that, because a severed
        utterance reports EXECUTE with a shorter payload.
        """
        decision = router._resolve_finalization(
            phrase.split(), hotword_active=False
        )
        assert decision.action is Action.DICTATE, (
            f"{phrase!r} executed mid-sentence instead of dictating: "
            f"payload={decision.payload!r} remainder={decision.remainder!r}"
        )
        assert decision.payload == phrase

    @pytest.mark.parametrize("phrase", _SEVEN_ENTRIES_WHOLE_UTTERANCE)
    def test_the_phrase_alone_still_executes(self, router, phrase):
        """Direction two: the command still works when spoken by itself.

        Without this half, flagging a pattern into permanent silence would
        satisfy every mid-sentence test in this file.
        """
        decision = router._resolve_finalization(
            phrase.split(), hotword_active=False
        )
        assert decision.action is Action.EXECUTE, (
            f"{phrase!r} stopped executing as a whole utterance: "
            f"action={decision.action} payload={decision.payload!r}"
        )
        assert decision.payload == phrase
