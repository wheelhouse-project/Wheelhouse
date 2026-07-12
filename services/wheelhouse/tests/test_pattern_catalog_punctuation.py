"""Pattern-catalog tests for STT-injected punctuation on the first word.

Slice wh-9f51.1: when the user says 'backspace comma', the STT (or ITN
post-processing) emits the literal 'backspace,' as a single token. The
PatternCatalog lookup must strip the trailing comma so the 'backspace'
command pattern still resolves -- otherwise the utterance falls through
to dictation and the literal 'backspace,' gets typed.

Inner punctuation must be preserved so escaped-literal first words like
'*cough*' still resolve.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

from speech.pattern_catalog import (
    PatternCatalog,
    PatternType,
    _normalize_lookup_word,
)


_HEADER = 'COMMAND_HOTWORD = "x-ray"\n\n'


def _write_patterns(tmp_path: Path, body: str) -> str:
    p = tmp_path / "patterns.toml"
    p.write_text(_HEADER + body, encoding="utf-8")
    return str(p)


# ----------------------------------------------------------------------
# _normalize_lookup_word direct unit tests
# ----------------------------------------------------------------------


class TestNormalizeLookupWord:
    """Verify the helper rstrips trailing sentence punctuation but
    preserves leading and inner punctuation so wrapper-style first
    words still work.

    wh-9f51.2.4: leading-strip was removed so the catalog and matcher
    agree on punctuation handling. The matcher only rstrips on the
    fullmatch retry path; if the catalog also stripped leading
    punctuation it would route ",backspace" into command buffering
    that the matcher cannot complete.
    """

    def test_strips_trailing_comma(self):
        assert _normalize_lookup_word("backspace,") == "backspace"

    def test_strips_trailing_period(self):
        assert _normalize_lookup_word("backspace.") == "backspace"

    def test_strips_trailing_semicolon(self):
        assert _normalize_lookup_word("backspace;") == "backspace"

    def test_strips_trailing_colon(self):
        assert _normalize_lookup_word("backspace:") == "backspace"

    def test_strips_trailing_exclamation(self):
        assert _normalize_lookup_word("backspace!") == "backspace"

    def test_strips_trailing_question(self):
        assert _normalize_lookup_word("backspace?") == "backspace"

    def test_does_not_strip_leading_punctuation(self):
        # wh-9f51.2.4: leading-strip removed. Symmetry with the matcher
        # (which only rstrips on its fullmatch retry path) matters more
        # than speculative leading-strip coverage.
        assert _normalize_lookup_word(",backspace") == ",backspace"

    def test_preserves_inner_punctuation_asterisk(self):
        # *cough* is an escaped-literal first word; the leading and
        # trailing asterisks are part of the token and must survive.
        assert _normalize_lookup_word("*cough*") == "*cough*"

    def test_preserves_hyphen(self):
        # Hyphens never appear in _PUNCT_STRIP; 'x-ray' stays intact.
        assert _normalize_lookup_word("x-ray") == "x-ray"

    def test_lowercases(self):
        assert _normalize_lookup_word("Backspace,") == "backspace"

    def test_empty_string(self):
        assert _normalize_lookup_word("") == ""

    def test_only_punctuation_returns_lowered_original(self):
        # Stripping would leave an empty string; fall back to the
        # lowercased original so the caller can still do a lookup
        # (which will of course miss, but won't crash).
        assert _normalize_lookup_word(",,,") == ",,,"


# ----------------------------------------------------------------------
# PatternCatalog lookup methods honour the normalization
# ----------------------------------------------------------------------


class TestCatalogLookupNormalization:
    """The lookup helpers (could_be_pattern_start, get_matching_patterns,
    get_pattern_type, get_trailing_command) must all normalize the
    incoming word so STT-attached punctuation doesn't drop the match."""

    def _backspace_catalog(self, tmp_path: Path) -> PatternCatalog:
        body = """
[[pattern]]
pattern = '''^back ?space\\s*(\\d+)?$'''
actions = [
    { function = "press", params = ["backspace", "g1"] }
]
"""
        return PatternCatalog(_write_patterns(tmp_path, body))

    def _cough_catalog(self, tmp_path: Path) -> PatternCatalog:
        body = """
[[pattern]]
pattern = '''\\*cough\\*'''
actions = [
    { function = "noop", params = [] }
]
"""
        return PatternCatalog(_write_patterns(tmp_path, body))

    def _submit_catalog(self, tmp_path: Path) -> PatternCatalog:
        body = """
[[pattern]]
pattern = '''submit'''
position = "trailing"
actions = [
    { function = "press_keys", params = ["enter"] }
]
"""
        return PatternCatalog(_write_patterns(tmp_path, body))

    def test_could_be_pattern_start_with_trailing_comma(self, tmp_path):
        catalog = self._backspace_catalog(tmp_path)
        assert catalog.could_be_pattern_start("backspace,") is True

    def test_could_be_pattern_start_with_trailing_period(self, tmp_path):
        catalog = self._backspace_catalog(tmp_path)
        assert catalog.could_be_pattern_start("backspace.") is True

    def test_could_be_pattern_start_with_cough_unchanged(self, tmp_path):
        """*cough* is registered; the lookup must NOT strip its asterisks."""
        catalog = self._cough_catalog(tmp_path)
        assert catalog.could_be_pattern_start("*cough*") is True

    def test_get_matching_patterns_with_trailing_comma_matches_bare(
        self, tmp_path,
    ):
        catalog = self._backspace_catalog(tmp_path)
        bare = catalog.get_matching_patterns("backspace")
        punctuated = catalog.get_matching_patterns("backspace,")
        assert bare, "fixture sanity: bare 'backspace' must produce matches"
        assert punctuated == bare

    def test_get_pattern_type_with_trailing_comma(self, tmp_path):
        catalog = self._backspace_catalog(tmp_path)
        assert catalog.get_pattern_type("backspace,") == PatternType.COMMAND

    def test_get_trailing_command_with_trailing_period(self, tmp_path):
        catalog = self._submit_catalog(tmp_path)
        entry = catalog.get_trailing_command("submit.")
        assert entry is not None
        assert entry["actions"] == [
            {"function": "press_keys", "params": ["enter"]}
        ]
