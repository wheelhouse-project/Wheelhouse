"""Guard: the local-model help speech patterns stay disabled.

The local-model help question-and-answer feature was abandoned (the small local
model was too slow and gave wrong or truncated answers). The local help chat
window is opened ONLY by the wheelhouse_help speech action, which is invoked
ONLY by the '^wheelhouse help$' and '^wheelhouse help (.+)$' patterns. Disabling
both patterns makes the local-model help unreachable by a user -- verified by an
exhaustive entry-point trace: no tray item, button, hotkey, startup default, or
unrecognized-command fallback opens that window. The online/browser help path
(wheelhouse_help_online) is intentionally kept.

These tests assert that intent so the local help cannot be re-enabled by
accident. The wheelhouse_help action function and the help window code stay in
the tree on purpose, so a future help path can reuse them.
"""

import tomllib
from pathlib import Path

import pytest

PATTERNS_PATH = Path(__file__).parent.parent / "speech" / "config" / "patterns.toml"


@pytest.fixture
def active_action_functions():
    """Every action function named by an ACTIVE (non-commented) pattern block.

    tomllib never sees commented-out blocks, so the parsed set is exactly the
    patterns that are live in the app.
    """
    with open(PATTERNS_PATH, "rb") as f:
        data = tomllib.load(f)
    funcs = []
    for block in data.get("pattern", []):
        for action in block.get("actions", []):
            func = action.get("function")
            if func:
                funcs.append(func)
    return funcs


def test_local_help_pattern_disabled(active_action_functions):
    # wheelhouse_help opens the abandoned local-model help chat window; no active
    # pattern may route to it.
    assert "wheelhouse_help" not in active_action_functions


def test_online_help_pattern_kept(active_action_functions):
    # The online/browser help path is intentionally retained.
    assert "wheelhouse_help_online" in active_action_functions
