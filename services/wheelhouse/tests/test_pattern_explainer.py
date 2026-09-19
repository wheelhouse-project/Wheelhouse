"""Tests for speech/pattern_explainer.py (wh-pattern-editor-explainer).

The explainer turns a pattern dict (the same per-pattern shape the manager
window receives from pm_get_patterns: raw_pattern, requires_hotword,
raw_actions, optional phrases list, optional type/position) into a
deterministic plain-English description (spec:
docs/plans/2026-07-09-pattern-manager-editor-design-v1.md section 10).

Guarantees enforced here:

1. Exact English: given a pattern, the output is an exact expected string.
   The output grammar is deliberately pinned so the Explain panel and the
   editor preview stay stable.
2. Never a wrong translation: regex constructs the translator does not
   understand fall back to quoting the raw expression, and a set of exotic
   regexes proves the fallback fires instead of a guess.
3. Full shipped coverage: every pattern in speech/config/patterns.toml
   produces an explanation, and the set of triggers that hit the
   raw-expression fallback is pinned to an explicit allowlist. That
   allowlist is not empty: FALLBACK_ALLOWLIST below holds the generated
   _va_range_fallbacks() entries, and every one
   carries its reason at the point it is defined.
4. Dependency-freeness: the module imports in a bare subprocess with all
   non-stdlib imports blocked except speech.action_catalog (same style as
   test_action_catalog.py), because the GUI process imports it.
"""
import re
import subprocess
import sys
import tomllib
import pytest
from pathlib import Path

from speech.pattern_explainer import (
    _UnsupportedConstruct,
    _parse_trigger,
    explain_pattern,
)

_TESTS_DIR = Path(__file__).parent
_SERVICE_DIR = _TESTS_DIR.parent
_PATTERNS_TOML = _SERVICE_DIR / "speech" / "config" / "patterns.toml"

HOTWORD = "x-ray"


class TestGridTriggers:
    @pytest.mark.parametrize("raw, expected", [
        (r"^((right|double)[\s-]+click\s+.+)$", "Say 'right click' (or 'double click') followed by any words."),
        (r"^((right|double)[\s-]+click[.!?]?)$", "Say 'right click' (or 'double click')."),
        (r"^((click|tap)[.!?]?)$", "Say 'click' (or 'tap')."),
        (r"^(mark[.!?]?)$", "Say 'mark'."),
        (r"^(drag[.!?]?)$", "Say 'drag'."),
        (r"^(move here[.!?]?)$", "Say 'move here'."),
        (r"^((1|2|3|4|5|6|7|8|9)[.!?]?)$", "Say a digit from 1 to 9."),
        (r"^((one|two|three|four|five|six|seven|eight|nine|too|to|for)[.!?]?)$", "Say a number from one to nine (also accepts 'too', 'to', 'for')."),
        (r"^((number|numbers)\s+(one|two|three|four|five|six|seven|eight|nine|too|to|for|1|2|3|4|5|6|7|8|9)[.!?]?)$", "Say 'number' (or 'numbers') followed by a number from one to nine, in words or digits (also accepts 'too', 'to', 'for')."),
    ])
    def test_exact_grid_trigger(self, raw, expected):
        assert explain_pattern(_pattern_dict(raw, []), HOTWORD).splitlines()[0] == expected

    @pytest.mark.parametrize("raw", [
        r"^click[.!?]? now$", r"^click[.!?]+$", r"^click[.!?]$",
        r"^right[\s-]*click$", r"^right[\s_]+click$",
        r"^((one|two|four|five|six|seven|eight|nine))$",
        r"^((one|two|three|four|five|six|seven|eight|nine|ten))$",
        r"^((1|2|3|4|5|6|7|8|9))? extra$",
        r"^((1|2|3|4|5|6|7|8|9)) times$",
        r"^number(1|2|3|4|5|6|7|8|9)$",
        r"^(1|2|3|4|5|6|7|8|9)(1|2|3|4|5|6|7|8|9)$",
        r"^((right|double)[\s-]+click\s+.+) again$",
        r"^(a|b|c|d|e|f|g)$",
    ])
    def test_unsupported_stays_honest(self, raw):
        assert FALLBACK_MARKER in explain_pattern(_pattern_dict(raw, []), HOTWORD)

    @pytest.mark.parametrize("missing", list("123456789"))
    def test_each_missing_digit_prevents_complete_range_claim(self, missing):
        raw = "^((" + "|".join(d for d in "123456789" if d != missing) + ")[.!?]?)$"
        assert FALLBACK_MARKER in explain_pattern(_pattern_dict(raw, []), HOTWORD)

    def test_range_wording_preserves_actual_accepted_forms(self):
        for pat in _shipped_patterns():
            if pat.get("doc_id") not in {
                "grid-number-word", "grid-number-digit", "grid-number-prefixed"
            }:
                continue
            raw = pat["pattern"]
            prefix = "number " if pat["doc_id"] == "grid-number-prefixed" else ""
            candidates = "one two three four five six seven eight nine too to for 1 2 3 4 5 6 7 8 9".split()
            text = explain_pattern(_pattern_dict(raw, []), HOTWORD)
            for token in candidates:
                expected = (
                    pat["doc_id"] == "grid-number-prefixed"
                    or (token.isdigit() == (pat["doc_id"] == "grid-number-digit"))
                )
                for punctuation in ("", ".", "!", "?"):
                    assert bool(re.fullmatch(raw, prefix + token + punctuation)) == expected
            assert len(text) < 230

    def test_large_literal_cross_product_remains_bounded(self):
        raw = "^" + "(one|two|three|four|five|six)" * 20 + "$"
        assert FALLBACK_MARKER in explain_pattern(_pattern_dict(raw, []), HOTWORD)

# The trigger-side fallback wording. Present exactly when the translator
# refused to translate and quoted the raw expression instead.
FALLBACK_MARKER = 'matching the expression "'


def _pattern_dict(raw_pattern, actions, requires_hotword=False, **extra):
    """Build a pattern dict in the pm_get_patterns entry shape."""
    d = {
        "raw_pattern": raw_pattern,
        "raw_actions": actions,
        "requires_hotword": requires_hotword,
    }
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# 0. Pattern-kind classification helper
# ---------------------------------------------------------------------------


class TestPatternKind:
    """pattern_kind is the single classification seam shared by the
    explainer and the manager window's Type badge
    (wh-pattern-editor-r4.2): trailing position wins, then an explicit
    type key, then the ^-anchor rule the runtime loader uses."""

    def test_precedence_position_then_type_then_anchor(self):
        from speech.pattern_explainer import pattern_kind

        assert pattern_kind(
            _pattern_dict("submit", [], position="trailing")
        ) == "trailing"
        assert pattern_kind(
            _pattern_dict(r"\bdeploy\b", [], type="replacement")
        ) == "replacement"
        assert pattern_kind(_pattern_dict("^save$", [])) == "command"
        assert pattern_kind(_pattern_dict(r"\bperiod\b", [])) == "replacement"


# ---------------------------------------------------------------------------
# 1. Exact-English cases
# ---------------------------------------------------------------------------


class TestPhrasesList:
    def test_phrase_list_renders_as_say_or_alternatives(self):
        pattern = _pattern_dict(
            r"^(?:editor|code\ editor|vs\ code)$",
            [{"function": "activate", "params": ["code.exe"]}],
            phrases=["editor", "code editor", "vs code"],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'editor' (or 'code editor', 'vs code').\n"
            "Switch to a window ('code.exe')."
        )

    def test_single_phrase_has_no_or_clause(self):
        pattern = _pattern_dict(
            r"^editor$",
            [{"function": "activate", "params": ["code.exe"]}],
            phrases=["editor"],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'editor'.\nSwitch to a window ('code.exe')."
        )


class TestWakeWordCommand:
    def test_plain_single_trigger_command_with_wake_word(self):
        # Shipped ^save$ pattern.
        pattern = _pattern_dict(
            "^save$",
            [{"function": "hk", "params": ["ctrl", "s"]}],
            requires_hotword=True,
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "You must say 'x-ray' first.\n"
            "Say 'save'.\n"
            "Press a hotkey (ctrl, s)."
        )

    def test_no_wake_word_sentence_when_not_required(self):
        pattern = _pattern_dict(
            "^zoom in$",
            [{"function": "hk", "params": ["ctrl", "+"]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'zoom in'.\nPress a hotkey (ctrl, +)."
        )

    def test_empty_hotword_degrades_to_generic_wake_sentence(self):
        pattern = _pattern_dict(
            "^save$",
            [{"function": "hk", "params": ["ctrl", "s"]}],
            requires_hotword=True,
        )
        assert explain_pattern(pattern, "").startswith(
            "You must say the wake word first.\n"
        )


class TestMultiStep:
    def test_steps_are_numbered_when_more_than_one(self):
        # Shipped ^delete word$ pattern.
        pattern = _pattern_dict(
            "^delete word$",
            [
                {"function": "hk", "params": ["ctrl", "left"],
                 "awaits_done": True},
                {"function": "hk", "params": ["shift", "ctrl", "right"],
                 "awaits_done": True},
                {"function": "hk", "params": ["del"], "awaits_done": True},
            ],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'delete word'.\n"
            "1. Press a hotkey (ctrl, left).\n"
            "2. Press a hotkey (shift, ctrl, right).\n"
            "3. Press a hotkey (del)."
        )

    def test_internal_steps_fold_to_brief_sentences(self):
        # Shipped ^search$ pattern: internal capture_clipboard folds, and
        # gs's magic 'capture_clipboard' param renders as the saved text.
        pattern = _pattern_dict(
            "^search$",
            [
                {"function": "hk", "params": ["ctrl", "c"],
                 "awaits_done": True},
                {"function": "capture_clipboard", "params": [],
                 "awaits_done": True},
                {"function": "gs", "params": ["capture_clipboard"]},
            ],
            requires_hotword=True,
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "You must say 'x-ray' first.\n"
            "Say 'search'.\n"
            "1. Press a hotkey (ctrl, c).\n"
            "2. Saves the clipboard text for a later step.\n"
            "3. Google search (the saved clipboard text)."
        )

    def test_skip_clipboard_restore_folds(self):
        # Shipped ^copy$ pattern.
        pattern = _pattern_dict(
            "^copy$",
            [
                {"function": "skip_clipboard_restore", "awaits_done": True},
                {"function": "hk", "params": ["ctrl", "c"],
                 "awaits_done": True},
            ],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'copy'.\n"
            "1. Keeps the copied text on the clipboard afterward.\n"
            "2. Press a hotkey (ctrl, c)."
        )


class TestReplacement:
    def test_single_text_replacement_folds_into_one_sentence(self):
        # Shipped \bperiod\b pattern. Spec section 10: replacements read
        # "When you say X anywhere while dictating, WheelHouse types Y
        # instead" -- unanchored patterns run in search mode and can match
        # mid-utterance (speech/pattern_matcher.py).
        pattern = _pattern_dict(
            r"\bperiod\b",
            [{"function": "text", "params": ["."]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "When you say 'period' anywhere while dictating, "
            "Wheelhouse types '.' instead."
        )

    def test_empty_text_replacement_reads_as_discard(self):
        # Shipped ^okay Google.*$ is anchored (a command); build an
        # unanchored equivalent to pin the replacement discard wording.
        pattern = _pattern_dict(
            r"\bscratch that\b",
            [{"function": "text", "params": [""]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "When you say 'scratch that' anywhere while dictating, "
            "Wheelhouse discards it (types nothing)."
        )

    def test_hyphen_aware_boundaries_translate_like_word_boundaries(self):
        # Shipped filter-filler-sounds pattern. It brackets its alternation
        # with (?<![\w-]) / (?![\w-]) instead of \b so a listed sound is not
        # matched inside a longer hyphenated token ("uh" inside "uh-huh").
        # Both assertions are zero-width and contribute no spoken content, so
        # the explanation must read exactly as the \b form would -- not fall
        # back to quoting the raw expression at the user.
        pattern = _pattern_dict(
            r"(?<![\w-])(?:mm-hmm|mm-mm|mhm|hmm|uh)(?![\w-])",
            [{"function": "text", "params": [""]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "When you say 'mm-hmm' (or 'mm-mm', 'mhm', 'hmm', 'uh') "
            "anywhere while dictating, Wheelhouse discards it "
            "(types nothing)."
        )

    def test_non_text_replacement_lists_steps_after_colon(self):
        # Shipped \bnew ?line\b pattern: optional-space variants expand.
        pattern = _pattern_dict(
            r"\bnew ?line\b",
            [{"function": "hk", "params": ["shift", "enter"]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "When you say 'new line' (or 'newline') anywhere while "
            "dictating:\n"
            "Press a hotkey (shift, enter)."
        )

    def test_replacement_with_capture_suffix(self):
        # Shipped \bparentheses(.*)$ pattern.
        pattern = _pattern_dict(
            r"\bparentheses(.*)$",
            [{"function": "wrap_or_insert", "params": ["(", ")", "g1"]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "When you say 'parentheses', optionally followed by any words, "
            "anywhere while dictating:\n"
            "Wrap in delimiters (left_fence: '('; right_fence: ')'; "
            "text: the words you say (g1))."
        )


class TestCapturePatterns:
    def test_capture_pattern_with_optional_letter_and_wake_word(self):
        # Shipped ^activates? (.+)$ pattern.
        pattern = _pattern_dict(
            r"^activates? (.+)$",
            [{"function": "activate", "params": ["g1"]}],
            requires_hotword=True,
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "You must say 'x-ray' first.\n"
            "Say 'activates' (or 'activate') followed by any words.\n"
            "Switch to a window (the words you say (g1))."
        )

    def test_optional_number_capture_names_repeat_param(self):
        # Shipped ^undo\s*(\d+)?$ pattern: hk's trailing g1 is the repeat.
        pattern = _pattern_dict(
            r"^undo\s*(\d+)?$",
            [{"function": "hk", "params": ["ctrl", "z", "g1"]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'undo', optionally followed by a number.\n"
            "Press a hotkey (keys: ctrl, z; repeat: the number you say (g1))."
        )

    def test_alternation_group_with_number(self):
        # Shipped ^(tab|indent)\s+(\d+)$ pattern.
        pattern = _pattern_dict(
            r"^(tab|indent)\s+(\d+)$",
            [{"function": "press", "params": ["tab", "g2"]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'tab' (or 'indent') followed by a number.\n"
            "Press one key (key: 'tab'; repeat: the number you say (g2))."
        )

    def test_embedded_group_reference_in_template(self):
        # Shipped ^go (.+) pattern: the template 'go g1' embeds the group.
        pattern = _pattern_dict(
            r"^go (.+)",
            [{"function": "cursor_navigate", "params": ["go g1"]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'go' followed by any words.\n"
            "Move the cursor by voice ('go g1' (g1 = the words you say))."
        )

    def test_letter_class_capture_is_not_described_as_any_words(self):
        # Shipped ^translate to ([a-z][a-z ]*)$ pattern. The capture takes
        # letters and spaces only, so borrowing (.+)'s "any words" would
        # promise the user something the expression refuses.
        pattern = _pattern_dict(
            r"^translate to ([a-z][a-z ]*)$",
            [{"function": "rewrite_text_ai", "params": ["Into g1."]}],
            requires_hotword=True,
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "You must say 'x-ray' first.\n"
            "Say 'translate to' followed by any words made of letters and "
            "spaces.\n"
            "Rewrite text with AI ('Into g1.' (g1 = the words you say))."
        )

    def test_a_letter_class_capture_that_may_be_empty_is_optional(self):
        # "[a-z ]*" alone matches nothing at all, so the words after the
        # phrase are optional rather than required.
        pattern = _pattern_dict(
            r"^dictate ([a-z ]*)$",
            [{"function": "text", "params": ["g1"]}],
        )
        assert "optionally followed by any words made of letters and spaces" \
            in explain_pattern(pattern, HOTWORD)

    def test_bare_dot_star_reads_as_optional_words(self):
        # Shipped ^okay Google.*$ pattern (anchored, so a command).
        pattern = _pattern_dict(
            r"^okay Google.*$",
            [{"function": "text", "params": [""]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'okay Google', optionally followed by any words.\n"
            "Types nothing (the matched words are discarded)."
        )


class TestTrailingPosition:
    def test_trailing_command_reads_as_last_word(self):
        # Shipped 'submit' pattern with position = "trailing".
        pattern = _pattern_dict(
            "submit",
            [{"function": "press_keys", "params": ["enter"]}],
            position="trailing",
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'submit' as the last word of what you say; the words you "
            "said before it are typed as dictation.\n"
            "Press a spoken key sequence ('enter')."
        )


# ---------------------------------------------------------------------------
# 2. Fallback: never a wrong translation
# ---------------------------------------------------------------------------


class TestRawExpressionFallback:
    def test_exotic_regex_falls_back_to_quoting_the_expression(self):
        raw = "^(?=zoom)[zZ]oom{1,2}$"
        pattern = _pattern_dict(
            raw, [{"function": "hk", "params": ["ctrl", "+"]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            'Say something matching the expression "^(?=zoom)[zZ]oom{1,2}$".\n'
            "Press a hotkey (ctrl, +)."
        )

    def test_unsupported_constructs_always_fall_back_never_guess(self):
        exotic = [
            r"^a|b$",                # top-level alternation
            r"^x[0-9]$",             # character class
            r"^(?P<n>foo)$",         # named group
            r"^\w+$",                # word-class shorthand
            r"^a{2}$",               # counted repetition
            r"^(.+) stop$",          # literal text AFTER a capture
            r"^so+n$",               # quantifier on a literal
            r"^back\S*$",            # non-space shorthand
            r"^pick ([0-9a-z]+)$",   # captured class that admits digits
            r"^pick ([a-z]{2})$",    # counted repetition inside a capture
            r"^pick ([a-z]+x)$",     # a literal riding along inside a capture
            # A range whose endpoints are both letters but whose span crosses
            # the ASCII gap between the cases. [A-z] matches [ \ ] ^ _ and
            # the backtick as well as letters, so calling it "letters and
            # spaces" would be a wrong translation, the one failure this
            # subsystem promises never to produce (wh-local-ai-runtime.2.27).
            r"^pick ([A-z]+)$",      # the full cross-case range
            r"^pick ([Z-a]+)$",      # a narrow range entirely inside the gap
            # Lookarounds stay unsupported in general. Only the two exact
            # hyphen-aware boundary tokens the shipped filler-sound pattern
            # uses are allowed through (they are \b with the hyphen removed
            # from the word set); anything else about a lookaround -- a
            # different character set, a literal, a lookahead that asserts
            # content -- must still fall back rather than be guessed at.
            r"^(?=zoom)zoom$",       # positive lookahead asserting a literal
            r"^(?<!a)b$",            # lookbehind on a different character
            r"^(?<![\w])b$",         # boundary-like, but not the exact token
            r"^b(?![\s-])$",         # lookahead over a different class
        ]
        for raw in exotic:
            pattern = _pattern_dict(
                raw, [{"function": "press", "params": ["esc"]}],
            )
            out = explain_pattern(pattern, HOTWORD)
            assert FALLBACK_MARKER in out, (
                f"expected raw-expression fallback for {raw!r}, got: {out}"
            )
            assert raw in out


# ---------------------------------------------------------------------------
# 3. Degradation (spec section 14: never crash)
# ---------------------------------------------------------------------------


class TestOptionalArticleCollapse:
    """The optional definite article does not multiply the variant count.

    wh-explainer-allowlist-landmarks. Each of the four landmark navigation
    patterns holds three groups -- (?:the )?, (?:beginning|start) and
    (?:the )? -- so the cross-product was 2 x 2 x 2 = 8, over _MAX_VARIANTS,
    and the Pattern Manager quoted a raw regular expression at the user for
    four ordinary navigation commands.

    David ruled on 2026-08-25 that the article collapses rather than the
    limit rising: raising _MAX_VARIANTS to 8 would print eight
    near-identical phrases per command, half of them unnatural English such
    as "go to start of word".
    """

    def test_a_landmark_pattern_with_two_articles_now_translates(self):
        variants, suffixes = _parse_trigger(
            r"^go to (?:the )?(?:beginning|start) of (?:the )?word$"
        )
        assert variants == [
            "go to the beginning of the word",
            "go to the start of the word",
        ]
        assert suffixes == []

    def test_the_article_free_form_is_no_longer_shown(self):
        """The accepted trade-off, pinned so a change to it is deliberate.

        The user no longer reads that "go to end" also works. Both forms
        still MATCH; only the display changes.
        """
        variants, _ = _parse_trigger(r"^go to (?:the )?end$")
        assert variants == ["go to the end"]

    def test_an_article_nested_in_another_group_also_collapses(self):
        """The collapse fires inside a branch parse, not only at top level.

        wh-explainer-allowlist-landmarks.1.1. _parse_trigger recurses into
        each alternation branch with _branch=True, so an optional article
        sitting INSIDE another group collapses during that recursion. Three
        shipped patterns reach this path and all three lost a displayed
        form in this change: "^maximize(?: (?:the )?window)?$" and its
        identically shaped minimize twin dropped "maximize window" /
        "minimize window", and "^(?:show (?:the )?)?desktop$" dropped
        "show desktop".

        Without this test, confining the collapse to top-level parses is
        invisible: all 43 tests in this module stay green while those three
        patterns silently regain the dropped forms. Measured, not assumed.
        """
        variants, _ = _parse_trigger(r"^maximize(?: (?:the )?window)?$")
        assert variants == ["maximize the window", "maximize"]

        variants, _ = _parse_trigger(r"^(?:show (?:the )?)?desktop$")
        assert variants == ["show the desktop", "desktop"]

    def test_an_optional_demonstrative_still_shows_both_forms(self):
        """Only the article collapses. "this " is a demonstrative.

        Measured over speech/config/patterns.toml: "the " is the only
        article appearing as an optional group body, in 30 places, and
        there is no optional "a " or "an ". The optional "this " appears in
        5 places and keeps both spoken forms, because collapsing it would
        drop a form the user genuinely says.
        """
        variants, _ = _parse_trigger(r"^select (?:this )?word$")
        assert variants == ["select this word", "select word"]


class TestDegradation:
    def test_unknown_function_degrades_to_bare_name(self):
        pattern = _pattern_dict(
            "^frob$",
            [{"function": "frobnicate", "params": ["a", 2]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'frob'.\nRuns frobnicate(a, 2)."
        )

    def test_empty_actions_says_so(self):
        pattern = _pattern_dict("^frob$", [])
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'frob'.\nThis pattern has no action steps."
        )

    def test_non_dict_actions_are_skipped(self):
        pattern = _pattern_dict(
            "^frob$",
            ["bogus", {"function": "press", "params": ["esc"]}],
        )
        assert explain_pattern(pattern, HOTWORD) == (
            "Say 'frob'.\nPress one key ('esc')."
        )

    def test_missing_fields_never_crash(self):
        assert isinstance(explain_pattern({}, HOTWORD), str)
        assert isinstance(explain_pattern({"raw_pattern": 5}, HOTWORD), str)
        assert isinstance(
            explain_pattern({"raw_pattern": "", "raw_actions": None}, HOTWORD),
            str,
        )


# ---------------------------------------------------------------------------
# 4. All-shipped-patterns coverage
# ---------------------------------------------------------------------------

# Spec section 10 demands the raw-fallback exceptions be enumerated. Almost
# every trigger expression shipped in speech/config/patterns.toml is
# translatable by the explainer (anchors, \b, literal words, optional
# letters/spaces, literal alternation groups incl. one nested optional
# group, (.+)/(.*), (\d+)/(\d+)?, \s+/\s*, bare trailing .*). If a future
# shipped pattern genuinely needs an exotic construct, add its exact
# expression here with a comment saying why it cannot be translated.
#
def _va_range_fallbacks() -> set:
    r"""The wh-voice-access-parity range captures (spliced 2026-08-24).

    100 triggers, four regular families: .1.5 go/move navigation, .1.4
    format-over-a-range, .1.3 delete/cut/copy-over-a-range, and .1.2
    select-by-range. Every one falls back for the same recorded reason as
    the remaining unsupported forms: an optional numeric capture ((\d+)? or
    (?: (\d+))?) that the translator cannot render as a friendly phrase
    yet -- the same number-range summarization gap tracked as
    wh-pattern-explainer-grid. Generated instead of hand-listed so the
    family shapes stay readable and a wording change fails this test
    loudly instead of drowning in a 100-line literal diff.
    """
    out = set()
    units = ["characters?", "lines?", "paragraphs?", "words?"]
    nav_pairs = [
        ("up", ["lines?", "paragraphs?"]),
        ("down", ["lines?", "paragraphs?"]),
        ("left", ["characters?", "words?"]),
        ("right", ["characters?", "words?"]),
    ]
    for direction, nav_units in nav_pairs:
        for unit in nav_units:
            out.add(rf"^(?:go|move) {direction}(?: (\d+))? {unit}$")
    for verb in ["bold", "italicize", "underline", "capitalize",
                 "lower ?case", "upper ?case"]:
        for unit in units:
            out.add(rf"^{verb} next (\d+)?\s*{unit}$")
            out.add(rf"^{verb} (?:previous|last) (\d+)?\s*{unit}$")
    for verb in ["delete", "cut", "copy"]:
        for direction in ["next", "previous", "last"]:
            for unit in units:
                out.add(rf"^{verb} {direction}\s+(\d+)?\s*{unit}$")
    for unit in units:
        out.add(rf"^select next\s+(\d+)?\s*{unit}$")
        out.add(rf"^select (?:previous|last)\s+(\d+)?\s*{unit}$")
    return out


FALLBACK_ALLOWLIST: set = _va_range_fallbacks()


def _shipped_patterns():
    with open(_PATTERNS_TOML, "rb") as fh:
        data = tomllib.load(fh)
    return data.get("pattern", [])


class TestShippedPatternCoverage:
    def test_every_shipped_pattern_produces_an_explanation(self):
        patterns = _shipped_patterns()
        assert len(patterns) >= 50, "patterns.toml unexpectedly small"
        for pat in patterns:
            entry = {
                "raw_pattern": pat.get("pattern", ""),
                "raw_actions": pat.get("actions", []),
                "requires_hotword": pat.get("requires_hotword", False),
            }
            if "position" in pat:
                entry["position"] = pat["position"]
            out = explain_pattern(entry, HOTWORD)
            assert isinstance(out, str) and out.strip(), (
                f"empty explanation for {pat.get('pattern')!r}"
            )
            assert "\r" not in out

    def test_trigger_fallbacks_match_the_pinned_allowlist(self):
        fallbacks = set()
        for pat in _shipped_patterns():
            entry = {
                "raw_pattern": pat.get("pattern", ""),
                "raw_actions": pat.get("actions", []),
                "requires_hotword": pat.get("requires_hotword", False),
            }
            if "position" in pat:
                entry["position"] = pat["position"]
            out = explain_pattern(entry, HOTWORD)
            if FALLBACK_MARKER in out:
                fallbacks.add(pat.get("pattern", ""))
        assert fallbacks == FALLBACK_ALLOWLIST, (
            "Shipped triggers hitting the raw-expression fallback changed. "
            "Either improve the translator or consciously extend "
            f"FALLBACK_ALLOWLIST. Unexpected: {sorted(fallbacks - FALLBACK_ALLOWLIST)}; "
            f"no longer falling back: {sorted(FALLBACK_ALLOWLIST - fallbacks)}"
        )


class TestShownPhrasesMatchTheirOwnPattern:
    """Every phrase the Pattern Manager prints must match its own pattern.

    This is the property the explainer owes the user: a person who says
    exactly the words on the screen has to get the command. Nothing checked
    it before wh-explainer-branch-space-loss, and 49 phrases across 27 of
    the shipped patterns failed it -- the screen said 'go to theend' for a
    pattern that only matches 'go to the end'.

    Two kinds of pattern are skipped, and each skip is counted so a change
    that quietly stops translating cannot hide here:

    - A pattern the translator refuses. It shows the raw expression, so
      there is no spoken phrase to check. TestShippedPatternCoverage pins
      that set separately.
    - A pattern with a suffix clause, from (.+), (\\d+) and friends. Its
      phrase is only the start of what the user says, so a whole-string
      match is the wrong check.
    """

    def _translated(self):
        """Yield (doc_id, raw, variants) for every pattern that translates."""
        for pat in _shipped_patterns():
            raw = pat.get("pattern", "")
            try:
                variants, suffixes = _parse_trigger(raw)
            except _UnsupportedConstruct:
                continue
            if suffixes:
                continue
            yield pat.get("doc_id", "<no doc_id>"), raw, variants

    def test_every_shown_phrase_matches_the_pattern_it_belongs_to(self):
        broken = []
        for doc_id, raw, variants in self._translated():
            expr = re.compile(raw, re.IGNORECASE)
            for variant in variants:
                if expr.fullmatch(variant) is None:
                    broken.append(f"{doc_id}: {variant!r} does not match {raw!r}")
        assert broken == [], (
            "The Pattern Manager shows spoken phrases that do not match "
            "their own pattern, so a user who says them gets nothing:\n  "
            + "\n  ".join(broken)
        )

    def test_the_check_covers_most_of_the_shipped_patterns(self):
        """Guard the guard: a translator that refused everything would make
        the test above pass over an empty set."""
        checked = list(self._translated())
        shipped = _shipped_patterns()
        assert len(checked) >= len(shipped) // 2, (
            f"only {len(checked)} of {len(shipped)} shipped patterns reached "
            "the phrase check; the translator or the skip rules changed"
        )

    def test_no_shown_phrase_has_stray_whitespace(self):
        """The whole-pattern cleanup must survive the branch fix."""
        bad = []
        for doc_id, _raw, variants in self._translated():
            for variant in variants:
                if variant != variant.strip() or "  " in variant:
                    bad.append(f"{doc_id}: {variant!r}")
        assert bad == [], "phrases with stray whitespace: " + ", ".join(bad)


# ---------------------------------------------------------------------------
# 5. Dependency-freeness (bare subprocess, stdlib + action_catalog only)
# ---------------------------------------------------------------------------


class TestDependencyFreeness:
    def test_imports_with_every_non_stdlib_module_blocked(self):
        """Import the module in a subprocess whose meta-path raises on any
        import that is neither stdlib nor the speech data modules. The GUI
        process imports pattern_explainer, so it must never pull the Logic
        import graph (same style as test_action_catalog.py)."""
        script = f"""
import sys
import importlib.abc

ALLOWED_LOCAL = {{
    "speech",
    "speech.action_catalog",
    "speech.pattern_explainer",
}}

class _BlockNonStdlib(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ALLOWED_LOCAL:
            return None
        root = fullname.partition(".")[0]
        if root in sys.stdlib_module_names:
            return None
        raise ModuleNotFoundError(
            "blocked for dependency-freeness test: " + fullname
        )

sys.meta_path.insert(0, _BlockNonStdlib())
sys.path.insert(0, {str(_SERVICE_DIR)!r})

from speech.pattern_explainer import explain_pattern

out = explain_pattern(
    {{
        "raw_pattern": "^save$",
        "requires_hotword": True,
        "raw_actions": [{{"function": "hk", "params": ["ctrl", "s"]}}],
    }},
    "x-ray",
)
assert "You must say 'x-ray' first." in out
print("IMPORT_OK")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(_SERVICE_DIR),
        )
        assert result.returncode == 0, result.stderr[-3000:]
        assert "IMPORT_OK" in result.stdout


class TestDescribeActions:
    """describe_actions() renders just the 'what happens' side of a pattern.

    The helpdoc command generator reuses this to derive a command's
    description from its actions instead of hand-authoring every one.
    """

    def test_single_action_is_one_sentence(self):
        from speech.pattern_explainer import describe_actions

        out = describe_actions([{"function": "hk", "params": ["ctrl", "s"]}])
        assert out == "Press a hotkey (ctrl, s)."

    def test_multiple_actions_are_a_numbered_list(self):
        from speech.pattern_explainer import describe_actions

        out = describe_actions(
            [
                {"function": "hk", "params": ["home"]},
                {"function": "hk", "params": ["shift", "end"]},
            ]
        )
        assert out == (
            "1. Press a hotkey (home).\n2. Press a hotkey (shift, end)."
        )

    def test_empty_actions_reports_no_steps(self):
        from speech.pattern_explainer import describe_actions

        assert describe_actions([]) == "This pattern has no action steps."

    def test_text_replacement_reads_as_types_instead(self):
        from speech.pattern_explainer import describe_actions

        out = describe_actions([{"function": "text", "params": [","]}])
        assert out == "Types ',' instead of the matched words."

    def test_non_list_input_never_crashes(self):
        from speech.pattern_explainer import describe_actions

        assert describe_actions(None) == "This pattern has no action steps."

    def test_non_dict_steps_are_skipped(self):
        from speech.pattern_explainer import describe_actions

        out = describe_actions([{"function": "hk", "params": ["esc"]}, "garbage"])
        assert out == "Press a hotkey (esc)."
