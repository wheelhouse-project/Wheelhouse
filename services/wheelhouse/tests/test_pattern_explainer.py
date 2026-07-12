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
   raw-expression fallback is pinned to an explicit allowlist (currently
   empty -- the translator handles every shipped construct).
4. Dependency-freeness: the module imports in a bare subprocess with all
   non-stdlib imports blocked except speech.action_catalog (same style as
   test_action_catalog.py), because the GUI process imports it.
"""
import subprocess
import sys
import tomllib
from pathlib import Path

from speech.pattern_explainer import explain_pattern

_TESTS_DIR = Path(__file__).parent
_SERVICE_DIR = _TESTS_DIR.parent
_PATTERNS_TOML = _SERVICE_DIR / "speech" / "config" / "patterns.toml"

HOTWORD = "x-ray"

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
            "WheelHouse types '.' instead."
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
            "WheelHouse discards it (types nothing)."
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

# Spec section 10 demands the raw-fallback exceptions be enumerated. Every
# trigger expression currently shipped in speech/config/patterns.toml is
# translatable by the explainer (anchors, \b, literal words, optional
# letters/spaces, literal alternation groups incl. one nested optional
# group, (.+)/(.*), (\d+)/(\d+)?, \s+/\s*, bare trailing .*), so the
# allowlist is EMPTY. If a future shipped pattern genuinely needs an exotic
# construct, add its exact expression here with a comment saying why it
# cannot be translated.
FALLBACK_ALLOWLIST: set = set()


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
