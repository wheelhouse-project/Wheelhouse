"""Shared fixtures for E2E speech pipeline tests."""
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def pattern_catalog():
    """Session-scoped PatternCatalog -- loaded once, shared across all E2E tests.

    user_patterns_file="" keeps the developer's personal
    data/user_patterns.toml out of the e2e catalog (hermeticity;
    wh-user-patterns-split.12.1). The session-scoped autouse guard in
    tests/conftest.py covers the default-resolution path too; the explicit
    argument here is the second line of defense.
    """
    wheelhouse_root = Path(__file__).parent.parent.parent
    sys.path.insert(0, str(wheelhouse_root))
    from speech.pattern_catalog import PatternCatalog
    patterns_path = str(wheelhouse_root / "speech" / "config" / "patterns.toml")
    return PatternCatalog(patterns_path, user_patterns_file="")
