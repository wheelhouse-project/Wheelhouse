"""Tests for WorkingIndicatorConfig.from_raw -- the never-raising validator
for the dictation-retraction working indicator's [dictation] config block
(wh-dictation-retraction-indicator.4).

Contract mirrors ClickConfig: never raises; a missing key falls back to its
default; a present key with a bad type degrades to the default and logs,
rather than crashing the GUI process.
"""

from __future__ import annotations

from working_indicator_config import WorkingIndicatorConfig


class TestWorkingIndicatorConfigFromRaw:
    def test_empty_block_defaults_to_enabled(self):
        """An absent key (empty [dictation] block) defaults the indicator ON."""
        cfg = WorkingIndicatorConfig.from_raw({})
        assert cfg.enabled is True

    def test_explicit_true_enables(self):
        cfg = WorkingIndicatorConfig.from_raw({"working_indicator_enabled": True})
        assert cfg.enabled is True

    def test_explicit_false_disables(self):
        """A valid operator opt-out is honored (degrade-by-user, not a fault)."""
        cfg = WorkingIndicatorConfig.from_raw({"working_indicator_enabled": False})
        assert cfg.enabled is False

    def test_non_bool_value_degrades_to_default(self):
        """A present-but-bad value degrades to the default and never raises."""
        # A string is not a bool.
        assert WorkingIndicatorConfig.from_raw(
            {"working_indicator_enabled": "yes"}
        ).enabled is True
        # bool-is-int trap: a raw int 1 must NOT be read as True.
        assert WorkingIndicatorConfig.from_raw(
            {"working_indicator_enabled": 1}
        ).enabled is True
        # int 0 must NOT be read as False either.
        assert WorkingIndicatorConfig.from_raw(
            {"working_indicator_enabled": 0}
        ).enabled is True

    def test_non_mapping_raw_is_safe(self):
        """A non-dict raw block (malformed config) never raises; default ON."""
        assert WorkingIndicatorConfig.from_raw(None).enabled is True
        assert WorkingIndicatorConfig.from_raw("nonsense").enabled is True
        assert WorkingIndicatorConfig.from_raw(42).enabled is True
