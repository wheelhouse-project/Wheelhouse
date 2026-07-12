"""The session-scoped e2e pattern catalog must never load the developer's
personal user patterns file (reviewer_0 finding wh-user-patterns-split.12.1).

The autouse hermeticity guard in tests/conftest.py was originally
function-scoped, but pytest instantiates higher-scoped fixtures first, so the
session-scoped ``pattern_catalog`` fixture was constructed BEFORE the guard
applied and silently merged data/user_patterns.toml into every e2e run. This
test pins the fix: the fixture itself must resolve to "no user file".
"""


def test_session_pattern_catalog_is_hermetic(pattern_catalog):
    assert pattern_catalog._user_patterns_file == ""
