# tests/test_pattern_manager_trigger.py
"""The Pattern Manager voice command accepts "pattern manager" as well as
"pattern"/"patterns" (wh-installer-pattern-trigger).

The graphical installer's Finish screen tells a first-time user to say
"x-ray pattern manager" to open the Pattern Manager. The shipped command
historically matched only ^patterns?$, so that phrase would not fire. This
test loads the real shipped patterns.toml and asserts the open_pattern_manager
command resolves for all three spoken phrases, with the hotword requirement
unchanged.

Spec: docs/superpowers/specs/2026-07-13-graphical-installer-wizard-design.md
section 10.
"""
from pathlib import Path

from speech.pattern_catalog import PatternCatalog

_SERVICE_DIR = Path(__file__).parent.parent
_SYSTEM_PATTERNS = _SERVICE_DIR / "speech" / "config" / "patterns.toml"


def _open_pattern_manager_entry(catalog):
    """The single shipped command whose action opens the Pattern Manager."""
    entries = [
        p
        for p in catalog.get_all_patterns()
        if any(a.get("function") == "open_pattern_manager" for a in p["actions"])
    ]
    assert len(entries) == 1, "expected exactly one open_pattern_manager command"
    return entries[0]


def test_pattern_manager_command_accepts_all_three_phrases(tmp_path):
    # Hermetic: point at a user file that does not exist, so only the shipped
    # patterns.toml is loaded.
    user_file = tmp_path / "user_patterns.toml"
    catalog = PatternCatalog(str(_SYSTEM_PATTERNS), user_patterns_file=str(user_file))

    entry = _open_pattern_manager_entry(catalog)
    compiled = entry["compiled_pattern"]

    # The new phrase the installer's Finish screen instructs the user to say.
    assert compiled.fullmatch("pattern manager")
    # The historical phrases still resolve (no regression).
    assert compiled.fullmatch("patterns")
    assert compiled.fullmatch("pattern")

    # Unchanged: it stays a hotword-gated command.
    assert entry["pattern_type"] == "command"
    assert entry["requires_hotword"] is True
