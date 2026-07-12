"""
Tests for the pattern test generator (tests/speech/generate_smoke_tests.py).

Covers wh-smoke-generator-crash:
1. generate_e2e_tests() crashed with IndexError on command patterns whose only
   action is press_keys: infer_action_type() maps press_keys to
   'hotkey_action', but the assertion-emitting branch searched for hk/hotkey
   actions and indexed [-1] into the empty result.
2. The e2e output is a scaffold for drafting tests for new patterns.
   services/wheelhouse/tests/e2e/test_e2e_all_patterns.py is hand-maintained
   (greedy end-marker handling, xfail markers); the generator must never
   write to it.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from tests.speech.generate_smoke_tests import (
    E2E_MAINTAINED_FILE,
    E2E_SCAFFOLD_FILE,
    generate_e2e_tests,
)

PRESS_KEYS_ONLY_TOML = r"""
[[pattern]]
pattern = '''^press\s*(.+)$'''
actions = [
    { function = "press_keys", params = ["g1"] }
]
"""


def test_press_keys_only_pattern_does_not_crash(tmp_path):
    """A command pattern whose only action is press_keys used to IndexError."""
    patterns_file = tmp_path / "patterns.toml"
    patterns_file.write_text(PRESS_KEYS_ONLY_TOML, encoding="utf-8")
    output_file = tmp_path / "scaffold.py"

    generate_e2e_tests(patterns_file=patterns_file, output_file=output_file)

    text = output_file.read_text(encoding="utf-8")
    assert "press" in text
    # No static key tuple exists for press_keys (the keys come from the spoken
    # words at runtime), so the generator falls back to the no-crash smoke
    # check instead of asserting specific keystrokes.
    assert "verify no crash" in text


def test_generator_runs_clean_on_shipped_patterns(tmp_path):
    r"""The committed patterns.toml includes ^press\s*(.+)$; must not crash."""
    output_file = tmp_path / "scaffold.py"

    generate_e2e_tests(output_file=output_file)

    assert output_file.exists()


def test_default_output_is_scaffold_not_maintained_file():
    """The maintained e2e file is hand-edited; the generator must not clobber it."""
    assert E2E_SCAFFOLD_FILE != E2E_MAINTAINED_FILE
    # pytest collects test_*.py; the scaffold must never be collected as tests.
    assert not E2E_SCAFFOLD_FILE.name.startswith("test_")
