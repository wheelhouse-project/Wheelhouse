"""Tests for ActionsConfig.from_raw (wh-actions-config).

The [actions] feature-init validator for the pattern-actions expansion
(spec: docs/superpowers/specs/2026-08-09-pattern-actions-expansion-design.md
section 6). ``ActionsConfig.from_raw`` validates a raw ``[actions]`` dict and
NEVER raises. Unlike ClickConfig there is no feature to disable: every key
degrades independently -- a missing key or a wrong-typed value falls back to
its default, and a right-typed but out-of-range value CLAMPS into the legal
range (the spec's "adjustable in config.toml but clamped below what the
message limit can carry").

Per-key rules:
* ``output_cap_chars`` -- real int (bool rejected), clamped into
  [1, OUTPUT_CAP_CEILING_CHARS]; the ceiling keeps a worst-case 4-bytes-per-
  char UTF-8 payload under the 64 KiB Logic-to-Input SharedMemory message
  (launcher.py SHARED_MEM_SIZE) with envelope headroom. Default 10000.
* ``run_capture_timeout_default_s`` -- real int or float (bool/NaN rejected),
  clamped into [0.1, 60.0] (the spec 2.3 waiting-action ceiling). Default 10.

Tests import with the services/wheelhouse root on sys.path:
``from speech.actions_config import ActionsConfig``.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from typing import Any

import pytest

from speech.actions_config import (
    DEFAULT_OUTPUT_CAP_CHARS,
    DEFAULT_RUN_CAPTURE_TIMEOUT_S,
    OUTPUT_CAP_CEILING_CHARS,
    RUN_CAPTURE_TIMEOUT_CEILING_S,
    RUN_CAPTURE_TIMEOUT_FLOOR_S,
    ActionsConfig,
)


class TestDefaults:
    """An absent or empty [actions] section yields the documented defaults."""

    def test_empty_dict_yields_defaults(self):
        config = ActionsConfig.from_raw({})
        assert config.output_cap_chars == 10000
        assert config.run_capture_timeout_default_s == 10.0

    def test_default_constants_match_spec(self):
        assert DEFAULT_OUTPUT_CAP_CHARS == 10000
        assert DEFAULT_RUN_CAPTURE_TIMEOUT_S == 10.0

    def test_missing_key_falls_back_per_key(self):
        config = ActionsConfig.from_raw({"output_cap_chars": 5000})
        assert config.output_cap_chars == 5000
        assert config.run_capture_timeout_default_s == DEFAULT_RUN_CAPTURE_TIMEOUT_S


class TestNonTableRaw:
    """ConfigService hands through whatever TOML held; a non-table [actions]
    value (``actions = 5``) or an absent section (the ``get`` default) must
    yield defaults without raising."""

    @pytest.mark.parametrize(
        "raw", [None, 5, "text", ["list"], 3.5, True, object()]
    )
    def test_non_dict_raw_yields_defaults(self, raw: Any):
        config = ActionsConfig.from_raw(raw)
        assert config.output_cap_chars == DEFAULT_OUTPUT_CAP_CHARS
        assert config.run_capture_timeout_default_s == DEFAULT_RUN_CAPTURE_TIMEOUT_S

    def test_non_dict_raw_logs_error(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.ERROR, logger="speech.actions_config"):
            ActionsConfig.from_raw("not a table")
        assert any("[actions]" in r.message for r in caplog.records)


class TestOutputCapChars:
    def test_valid_value_passes_through(self):
        config = ActionsConfig.from_raw({"output_cap_chars": 2000})
        assert config.output_cap_chars == 2000

    def test_ceiling_value_passes_through_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ):
        # The exact boundary is legal: no clamp, no warning. The clamp-from-
        # outside tests always fire the warning path, so only a dedicated
        # boundary test can catch a regression that clamps or warns on the
        # boundary value itself.
        with caplog.at_level(logging.WARNING, logger="speech.actions_config"):
            config = ActionsConfig.from_raw(
                {"output_cap_chars": OUTPUT_CAP_CEILING_CHARS}
            )
        assert config.output_cap_chars == OUTPUT_CAP_CEILING_CHARS
        assert not caplog.records

    def test_floor_value_passes_through_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING, logger="speech.actions_config"):
            config = ActionsConfig.from_raw({"output_cap_chars": 1})
        assert config.output_cap_chars == 1
        assert not caplog.records

    def test_over_ceiling_clamps_to_ceiling(self):
        config = ActionsConfig.from_raw({"output_cap_chars": 1_000_000})
        assert config.output_cap_chars == OUTPUT_CAP_CEILING_CHARS

    @pytest.mark.parametrize("value", [0, -5])
    def test_below_one_clamps_to_one(self, value: int):
        config = ActionsConfig.from_raw({"output_cap_chars": value})
        assert config.output_cap_chars == 1

    def test_clamp_logs_warning_naming_key(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.WARNING, logger="speech.actions_config"):
            ActionsConfig.from_raw({"output_cap_chars": 1_000_000})
        assert any("output_cap_chars" in r.message for r in caplog.records)

    @pytest.mark.parametrize("value", [True, False, "big", 1.5, None, [10000]])
    def test_wrong_type_degrades_to_default(self, value: Any):
        config = ActionsConfig.from_raw({"output_cap_chars": value})
        assert config.output_cap_chars == DEFAULT_OUTPUT_CAP_CHARS

    def test_wrong_type_logs_error_naming_key(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.ERROR, logger="speech.actions_config"):
            ActionsConfig.from_raw({"output_cap_chars": "big"})
        assert any("output_cap_chars" in r.message for r in caplog.records)

    def test_ceiling_keeps_worst_case_payload_under_message_limit(self):
        # The cap counts characters; a character is up to 4 bytes in UTF-8.
        # The whole worst-case text must fit in the 64 KiB Logic-to-Input
        # SharedMemory message (launcher.py SHARED_MEM_SIZE) AFTER reserving
        # the documented 4 KiB for the pickled command envelope around it --
        # asserting only against the raw 64 KiB would let the envelope
        # reserve silently shrink while the test stays green.
        assert OUTPUT_CAP_CEILING_CHARS * 4 <= 64 * 1024 - 4 * 1024

    def test_wrong_type_does_not_affect_other_key(self):
        config = ActionsConfig.from_raw(
            {"output_cap_chars": "big", "run_capture_timeout_default_s": 20}
        )
        assert config.output_cap_chars == DEFAULT_OUTPUT_CAP_CHARS
        assert config.run_capture_timeout_default_s == 20.0


class TestRunCaptureTimeoutDefault:
    def test_valid_float_passes_through(self):
        config = ActionsConfig.from_raw({"run_capture_timeout_default_s": 2.5})
        assert config.run_capture_timeout_default_s == 2.5

    def test_valid_int_promotes_to_float(self):
        config = ActionsConfig.from_raw({"run_capture_timeout_default_s": 20})
        assert config.run_capture_timeout_default_s == 20.0
        assert isinstance(config.run_capture_timeout_default_s, float)

    def test_over_ceiling_clamps_to_sixty(self):
        config = ActionsConfig.from_raw({"run_capture_timeout_default_s": 120})
        assert config.run_capture_timeout_default_s == RUN_CAPTURE_TIMEOUT_CEILING_S
        assert RUN_CAPTURE_TIMEOUT_CEILING_S == 60.0

    @pytest.mark.parametrize("value", [0, 0.05, -3])
    def test_below_floor_clamps_to_floor(self, value: Any):
        config = ActionsConfig.from_raw({"run_capture_timeout_default_s": value})
        assert config.run_capture_timeout_default_s == RUN_CAPTURE_TIMEOUT_FLOOR_S
        assert RUN_CAPTURE_TIMEOUT_FLOOR_S == 0.1

    def test_clamp_logs_warning_naming_key(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.WARNING, logger="speech.actions_config"):
            ActionsConfig.from_raw({"run_capture_timeout_default_s": 120})
        assert any(
            "run_capture_timeout_default_s" in r.message for r in caplog.records
        )

    def test_floor_value_passes_through_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ):
        # The exact boundary is legal: no clamp, no warning (see the matching
        # output_cap_chars boundary tests).
        with caplog.at_level(logging.WARNING, logger="speech.actions_config"):
            config = ActionsConfig.from_raw(
                {"run_capture_timeout_default_s": 0.1}
            )
        assert config.run_capture_timeout_default_s == 0.1
        assert not caplog.records

    @pytest.mark.parametrize("value", [60, 60.0])
    def test_ceiling_value_passes_through_without_warning(
        self, value: Any, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING, logger="speech.actions_config"):
            config = ActionsConfig.from_raw(
                {"run_capture_timeout_default_s": value}
            )
        assert config.run_capture_timeout_default_s == 60.0
        assert not caplog.records

    def test_positive_infinity_clamps_to_ceiling(
        self, caplog: pytest.LogCaptureFixture
    ):
        # TOML has literal inf/-inf values and tomllib hands them through as
        # Python floats; math.isnan(inf) is False, so infinity takes the
        # clamp-and-warn path, not the degrade-to-default path.
        with caplog.at_level(logging.WARNING, logger="speech.actions_config"):
            config = ActionsConfig.from_raw(
                {"run_capture_timeout_default_s": math.inf}
            )
        assert config.run_capture_timeout_default_s == RUN_CAPTURE_TIMEOUT_CEILING_S
        assert any(
            "run_capture_timeout_default_s" in r.message for r in caplog.records
        )

    def test_negative_infinity_clamps_to_floor(self):
        config = ActionsConfig.from_raw(
            {"run_capture_timeout_default_s": -math.inf}
        )
        assert config.run_capture_timeout_default_s == RUN_CAPTURE_TIMEOUT_FLOOR_S

    def test_huge_int_clamps_to_ceiling(self, caplog: pytest.LogCaptureFixture):
        # tomllib parses TOML integers at arbitrary precision, and an int too
        # large for float() raises OverflowError from both math.isnan and the
        # float conversion. The clamp must compare BEFORE converting -- a huge
        # timeout is a right-typed out-of-range value, so the contract is
        # clamp-and-warn, not degrade-to-default.
        import tomllib

        parsed = tomllib.loads(
            "run_capture_timeout_default_s = " + "9" * 400
        )
        with caplog.at_level(logging.WARNING, logger="speech.actions_config"):
            config = ActionsConfig.from_raw(parsed)
        assert config.run_capture_timeout_default_s == RUN_CAPTURE_TIMEOUT_CEILING_S
        assert any(
            "run_capture_timeout_default_s" in r.getMessage()
            for r in caplog.records
        )

    def test_huge_negative_int_clamps_to_floor(self):
        config = ActionsConfig.from_raw(
            {"run_capture_timeout_default_s": -(10**400)}
        )
        assert config.run_capture_timeout_default_s == RUN_CAPTURE_TIMEOUT_FLOOR_S

    def test_huge_int_does_not_affect_other_key(self):
        # The per-key degrade contract: one key's extreme value must never
        # discard the other key's valid value via the broad exception fallback.
        config = ActionsConfig.from_raw(
            {"output_cap_chars": 5000, "run_capture_timeout_default_s": 10**400}
        )
        assert config.output_cap_chars == 5000
        assert config.run_capture_timeout_default_s == RUN_CAPTURE_TIMEOUT_CEILING_S

    @pytest.mark.parametrize(
        "value", [True, False, "ten", None, [10], math.nan]
    )
    def test_wrong_type_or_nan_degrades_to_default(self, value: Any):
        config = ActionsConfig.from_raw({"run_capture_timeout_default_s": value})
        assert config.run_capture_timeout_default_s == DEFAULT_RUN_CAPTURE_TIMEOUT_S

    def test_wrong_type_logs_error_naming_key(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.ERROR, logger="speech.actions_config"):
            ActionsConfig.from_raw({"run_capture_timeout_default_s": "ten"})
        assert any(
            "run_capture_timeout_default_s" in r.message for r in caplog.records
        )


class TestShape:
    def test_frozen_dataclass(self):
        config = ActionsConfig.from_raw({})
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.output_cap_chars = 1  # type: ignore[misc]

    def test_unknown_keys_ignored(self):
        # A future key added to a user's config.toml before this build knows
        # it must not break the two shipped keys.
        config = ActionsConfig.from_raw({"future_key": 42})
        assert config.output_cap_chars == DEFAULT_OUTPUT_CAP_CHARS
        assert config.run_capture_timeout_default_s == DEFAULT_RUN_CAPTURE_TIMEOUT_S
