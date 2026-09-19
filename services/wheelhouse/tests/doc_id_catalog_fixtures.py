"""Catalog-derived inputs; synthetic user edits stay explicit in each test.

Read on every call and return fresh data so an edit cannot contaminate another
fixture. Select by the shipped doc_id, never by file position or regex text.
"""

from pathlib import Path
import tomllib

import tomli_w

from speech.pattern_catalog import PatternCatalog


CATALOG_PATH = Path(__file__).resolve().parents[1] / "speech/config/patterns.toml"


def shipped_entry(doc_id, *, source=CATALOG_PATH):
    entries = tomllib.loads(source.read_text(encoding="utf-8"))["pattern"]
    matches = [entry for entry in entries if entry.get("doc_id") == doc_id]
    if len(matches) != 1:
        raise ValueError(f"Expected one shipped {doc_id!r} in {source}, got {len(matches)}")
    return matches[0]


def block(entry):
    return tomli_w.dumps({"pattern": [entry]})


def rewritten(entry):
    """A release changes regex text, preserving its language and other fields."""
    pattern = entry["pattern"]
    # The catalog classifies commands by the leading ^, before compilation.
    if pattern.startswith("^"):
        expression = "^(?:" + pattern[1:] + ")"
    else:
        expression = "(?:" + pattern + ")"
    return {**entry, "pattern": expression}


def runtime_entry(doc_id, tmp_path, *, rewrite=False):
    entry = shipped_entry(doc_id)
    if rewrite:
        entry = rewritten(entry)
    system = tmp_path / f"{doc_id}-system.toml"
    user = tmp_path / f"{doc_id}-user.toml"
    system.write_text('COMMAND_HOTWORD = "x-ray"\n' + block(entry), encoding="utf-8")
    user.write_text('COMMAND_HOTWORD = "x-ray"\n', encoding="utf-8")
    catalog = PatternCatalog(str(system), str(user))
    [built] = catalog.get_all_patterns()
    return built


MAXIMIZE = shipped_entry("maximize-window")
RESTORE = shipped_entry("restore-window")
MAX_PATTERN = MAXIMIZE["pattern"]
RESTORE_PATTERN = RESTORE["pattern"]
BUILTIN_BEFORE = block(MAXIMIZE)
BUILTIN_AFTER = block(rewritten(MAXIMIZE))
USER_OVERRIDE = block({
    **MAXIMIZE,
    "actions": [{"function": "hk", "params": ["ctrl", "alt", "m"]}],
})
