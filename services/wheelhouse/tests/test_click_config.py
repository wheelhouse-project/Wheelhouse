"""Tests for ClickConfig.from_raw (wh-1yqgn).

The voice-clicking feature-init validator. ``ClickConfig.from_raw`` validates a
raw ``[click]`` dict against the v5 design doc's VALIDATION RULES TABLE (16
keys) and NEVER raises: on the first type/range failure it logs an error
naming the offending key and returns a disabled config recording that key.

Two distinct disabled shapes:
* degrade-by-validation: ``enabled=False`` AND ``invalid_key=<key>``.
* degrade-by-user: a valid ``enabled=false`` returns ``enabled=False`` AND
  ``invalid_key=None`` (the user opted out; nothing failed).

Missing-key policy (documented, tested consistently): a MISSING key falls back
to its v5 default. A missing key is config-author omission, not a malformed
value, so it does not disable the feature. Only a PRESENT key with a bad
type/range disables.

bool-is-int trap: ``bool`` is a subclass of ``int`` in Python. int-typed keys
reject a ``bool`` (so ``True`` is not silently accepted as ``1``); bool-typed
keys reject a non-``bool`` int/float. float-typed keys accept a real ``int``
(promoted) but reject ``bool``.

Tests import with the services/wheelhouse root on sys.path:
``from ui.click_config import ClickConfig, DISABLED_CLICK_CONFIG``.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
from typing import Any

from ui.click_config import ClickConfig, DISABLED_CLICK_CONFIG

# A fully-valid raw [click] dict at the v5 defaults. Every test that wants a
# valid baseline copies this and mutates one key.
VALID_RAW: dict[str, Any] = {
    "enabled": True,
    "use_focus_targeting": True,
    "enable_offmonitor_fallback": False,
    "min_confidence": 0.4,
    "clear_winner_margin": 0.15,
    "tiebreaker_min_separation_logical_px": 30,
    "tiebreaker_influence_logical_px": 400,
    "notice_max_names": 3,
    "enable_screen_reader_flag": False,
    "snapshot_ttl_seconds": 30,
    "response_timeout_ms": 3000,
    "walk_deadline_ms": 2500,
    "min_substring_query_length": 4,
    "min_substring_overlap_ratio": 0.6,
    "enable_coordinate_click_on_com_error": False,
    "browser_processes": ["brave.exe", "chrome.exe"],
    "browser_processes_extend": [],
}


def raw(**overrides: Any) -> dict[str, Any]:
    base = copy.deepcopy(VALID_RAW)
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# (a) Fully-valid round-trip.
# ---------------------------------------------------------------------------

def test_valid_raw_round_trips_every_key():
    cfg = ClickConfig.from_raw(raw())
    assert cfg.enabled is True
    assert cfg.use_focus_targeting is True
    assert cfg.enable_offmonitor_fallback is False
    assert cfg.min_confidence == 0.4
    assert cfg.clear_winner_margin == 0.15
    assert cfg.tiebreaker_min_separation_logical_px == 30
    assert cfg.tiebreaker_influence_logical_px == 400
    assert cfg.notice_max_names == 3
    assert cfg.enable_screen_reader_flag is False
    assert cfg.snapshot_ttl_seconds == 30
    assert cfg.response_timeout_ms == 3000
    assert cfg.walk_deadline_ms == 2500
    assert cfg.min_substring_query_length == 4
    assert cfg.min_substring_overlap_ratio == 0.6
    assert cfg.enable_coordinate_click_on_com_error is False
    assert cfg.browser_processes == ("brave.exe", "chrome.exe")
    assert cfg.browser_processes_extend == ()
    assert cfg.invalid_key is None


def test_list_fields_become_tuples():
    cfg = ClickConfig.from_raw(
        raw(browser_processes=["a.exe"], browser_processes_extend=["b.exe"])
    )
    assert isinstance(cfg.browser_processes, tuple)
    assert isinstance(cfg.browser_processes_extend, tuple)
    assert cfg.browser_processes == ("a.exe",)
    assert cfg.browser_processes_extend == ("b.exe",)


# ---------------------------------------------------------------------------
# (b) Numeric out-of-range -> disabled with invalid_key set.
# ---------------------------------------------------------------------------

def _assert_disabled(cfg: ClickConfig, key: str) -> None:
    assert cfg.enabled is False
    assert cfg.invalid_key == key


def test_min_confidence_below_range_disables():
    _assert_disabled(ClickConfig.from_raw(raw(min_confidence=-0.1)), "min_confidence")


def test_min_confidence_above_range_disables():
    _assert_disabled(ClickConfig.from_raw(raw(min_confidence=1.1)), "min_confidence")


def test_clear_winner_margin_out_of_range_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(clear_winner_margin=2.0)), "clear_winner_margin"
    )


def test_min_substring_overlap_ratio_out_of_range_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(min_substring_overlap_ratio=1.5)),
        "min_substring_overlap_ratio",
    )


def test_tiebreaker_min_separation_negative_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(tiebreaker_min_separation_logical_px=-1)),
        "tiebreaker_min_separation_logical_px",
    )


def test_tiebreaker_influence_negative_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(tiebreaker_influence_logical_px=-5)),
        "tiebreaker_influence_logical_px",
    )


def test_notice_max_names_below_one_disables():
    _assert_disabled(ClickConfig.from_raw(raw(notice_max_names=0)), "notice_max_names")


def test_snapshot_ttl_below_one_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(snapshot_ttl_seconds=0)), "snapshot_ttl_seconds"
    )


def test_response_timeout_below_100_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(response_timeout_ms=99)), "response_timeout_ms"
    )


def test_min_substring_query_length_below_one_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(min_substring_query_length=0)),
        "min_substring_query_length",
    )


# ---------------------------------------------------------------------------
# walk_deadline_ms (wh-9f3t.54.2): int >= 100 AND <= the effective
# response_timeout_ms, so the Input-side UIA click walk gives up no later than
# the Logic-side click awaiter. Missing -> default 2500 (strictly < the
# response_timeout_ms default of 3000). Out of range / above the awaiter
# timeout -> disabled with invalid_key set (never-raise pattern).
# ---------------------------------------------------------------------------

def test_walk_deadline_default_when_missing():
    base = copy.deepcopy(VALID_RAW)
    del base["walk_deadline_ms"]
    cfg = ClickConfig.from_raw(base)
    assert cfg.enabled is True
    assert cfg.invalid_key is None
    assert cfg.walk_deadline_ms == 2500


def test_walk_deadline_valid_value_round_trips():
    cfg = ClickConfig.from_raw(raw(walk_deadline_ms=1500))
    assert cfg.enabled is True
    assert cfg.walk_deadline_ms == 1500


def test_walk_deadline_below_floor_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(walk_deadline_ms=99)), "walk_deadline_ms"
    )


def test_walk_deadline_above_response_timeout_disables():
    # walk_deadline_ms must be STRICTLY < the effective response_timeout_ms so
    # the Input walk never outlives the Logic awaiter. 3001 > 3000 -> disabled.
    _assert_disabled(
        ClickConfig.from_raw(raw(walk_deadline_ms=3001)), "walk_deadline_ms"
    )


def test_walk_deadline_equal_to_response_timeout_disables():
    # FINDING 3: the upper bound is STRICT (open). An explicit walk_deadline_ms
    # EQUAL to response_timeout_ms leaves zero slack for the pre-walk latency
    # (the awaiter starts at IPC send; the walk deadline starts only after the
    # SharedMemory round-trip), so equality is rejected, not accepted.
    _assert_disabled(
        ClickConfig.from_raw(raw(response_timeout_ms=3000, walk_deadline_ms=3000)),
        "walk_deadline_ms",
    )


def test_walk_deadline_within_margin_band_disables():
    # wh-9f3t.74.1: the explicit path enforces the SAME margin the missing-key
    # clamp applies, not just strict-less-than. An explicit value within the
    # margin band (>= response_timeout_ms - _WALK_DEADLINE_MARGIN_MS, i.e. >=
    # 2750 at the 3000 default) leaves too little slack for the pre-walk
    # latency, so it is rejected. 2999 used to be accepted; it now disables.
    _assert_disabled(
        ClickConfig.from_raw(raw(response_timeout_ms=3000, walk_deadline_ms=2999)),
        "walk_deadline_ms",
    )
    _assert_disabled(
        ClickConfig.from_raw(raw(response_timeout_ms=3000, walk_deadline_ms=2750)),
        "walk_deadline_ms",
    )


def test_walk_deadline_just_below_margin_ceiling_accepted():
    # The largest accepted explicit value is response_timeout_ms - margin - 1
    # (2749 at the 3000 default / 250ms margin).
    cfg = ClickConfig.from_raw(raw(response_timeout_ms=3000, walk_deadline_ms=2749))
    assert cfg.enabled is True
    assert cfg.walk_deadline_ms == 2749


def test_walk_deadline_validated_against_custom_response_timeout():
    # When response_timeout_ms is raised, a walk_deadline_ms below the margin
    # ceiling (response_timeout_ms - margin = 4750) is accepted; the bound
    # tracks the effective awaiter timeout AND keeps the margin.
    cfg = ClickConfig.from_raw(raw(response_timeout_ms=5000, walk_deadline_ms=4500))
    assert cfg.enabled is True
    assert cfg.walk_deadline_ms == 4500


def test_walk_deadline_bool_disables():
    # bool-is-int trap: an int-typed key rejects a bool.
    _assert_disabled(
        ClickConfig.from_raw(raw(walk_deadline_ms=True)), "walk_deadline_ms"
    )


def test_walk_deadline_at_floor_accepted():
    cfg = ClickConfig.from_raw(raw(walk_deadline_ms=100))
    assert cfg.enabled is True
    assert cfg.walk_deadline_ms == 100


def test_missing_walk_deadline_clamped_below_tightened_response_timeout():
    """Cross-key invariant must hold even when walk_deadline_ms is UNSET.

    A user who tightens response_timeout_ms (e.g. 1000) below the 2500
    walk_deadline_ms default, WITHOUT setting walk_deadline_ms, must NOT get an
    ineffective bound (walk_deadline_ms 2500 > awaiter 1000). The missing-key
    default is clamped DOWN to STRICTLY BELOW response_timeout_ms (leaving a
    ~250ms margin), and clicking stays ENABLED (a lowered awaiter timeout is a
    sensible config, not a fault). FINDING 3.
    """
    base = copy.deepcopy(VALID_RAW)
    base["response_timeout_ms"] = 1000
    del base["walk_deadline_ms"]
    cfg = ClickConfig.from_raw(base)
    assert cfg.enabled is True
    assert cfg.invalid_key is None
    assert cfg.response_timeout_ms == 1000
    # The bound is effective AND strictly below the awaiter (margin preserved).
    assert cfg.walk_deadline_ms < cfg.response_timeout_ms
    # min(2500, 1000 - 250) floored at 100 -> 750.
    assert cfg.walk_deadline_ms == 750


def test_missing_walk_deadline_floored_when_response_timeout_at_minimum():
    """At the tightest legal awaiter (response_timeout_ms == 100) the margin
    would push the clamp negative, so it floors at 100 and clicking stays
    enabled. This is the one degenerate case where equality is unavoidable --
    there is no room below the 100ms floor."""
    base = copy.deepcopy(VALID_RAW)
    base["response_timeout_ms"] = 100
    del base["walk_deadline_ms"]
    cfg = ClickConfig.from_raw(base)
    assert cfg.enabled is True
    assert cfg.walk_deadline_ms == 100


def test_missing_walk_deadline_keeps_default_when_response_timeout_above_it():
    """When response_timeout_ms is at/above the 2500 default, the missing-key
    clamp leaves the default untouched (it only ever lowers, never raises)."""
    base = copy.deepcopy(VALID_RAW)
    base["response_timeout_ms"] = 5000
    del base["walk_deadline_ms"]
    cfg = ClickConfig.from_raw(base)
    assert cfg.enabled is True
    assert cfg.walk_deadline_ms == 2500
    assert cfg.walk_deadline_ms <= cfg.response_timeout_ms


# ---------------------------------------------------------------------------
# (b2) Numeric AT-boundary -> ACCEPTED (closed bounds, wh-9f3t.32.1).
#
# The numeric validators use closed bounds (>=, <=). The below-boundary
# rejection tests above prove the reject side; these prove the exact-boundary
# value is ACCEPTED. Without them a regression from ``>= minimum`` to
# ``> minimum`` in _is_int_at_least, or from ``0.0 <= v <= 1.0`` to
# ``0.0 < v < 1.0`` in _is_unit_float, would pass the suite undetected.
# ---------------------------------------------------------------------------

def _assert_enabled_with(cfg: ClickConfig, key: str, value: Any) -> None:
    assert cfg.enabled is True
    assert cfg.invalid_key is None
    assert getattr(cfg, key) == value


# Unit floats: BOTH ends 0.0 and 1.0 are in the closed [0.0, 1.0] range.

def test_min_confidence_at_lower_bound_accepted():
    _assert_enabled_with(
        ClickConfig.from_raw(raw(min_confidence=0.0)), "min_confidence", 0.0
    )


def test_min_confidence_at_upper_bound_accepted():
    _assert_enabled_with(
        ClickConfig.from_raw(raw(min_confidence=1.0)), "min_confidence", 1.0
    )


def test_clear_winner_margin_at_lower_bound_accepted():
    _assert_enabled_with(
        ClickConfig.from_raw(raw(clear_winner_margin=0.0)), "clear_winner_margin", 0.0
    )


def test_clear_winner_margin_at_upper_bound_accepted():
    _assert_enabled_with(
        ClickConfig.from_raw(raw(clear_winner_margin=1.0)), "clear_winner_margin", 1.0
    )


def test_min_substring_overlap_ratio_at_lower_bound_accepted():
    _assert_enabled_with(
        ClickConfig.from_raw(raw(min_substring_overlap_ratio=0.0)),
        "min_substring_overlap_ratio",
        0.0,
    )


def test_min_substring_overlap_ratio_at_upper_bound_accepted():
    _assert_enabled_with(
        ClickConfig.from_raw(raw(min_substring_overlap_ratio=1.0)),
        "min_substring_overlap_ratio",
        1.0,
    )


# >=0 ints: the exact minimum 0 is accepted.

def test_tiebreaker_min_separation_at_zero_accepted():
    _assert_enabled_with(
        ClickConfig.from_raw(raw(tiebreaker_min_separation_logical_px=0)),
        "tiebreaker_min_separation_logical_px",
        0,
    )


def test_tiebreaker_influence_at_zero_accepted():
    _assert_enabled_with(
        ClickConfig.from_raw(raw(tiebreaker_influence_logical_px=0)),
        "tiebreaker_influence_logical_px",
        0,
    )


# >=1 ints: the exact minimum 1 is accepted.

def test_notice_max_names_at_one_accepted():
    _assert_enabled_with(
        ClickConfig.from_raw(raw(notice_max_names=1)), "notice_max_names", 1
    )


def test_snapshot_ttl_at_one_accepted():
    _assert_enabled_with(
        ClickConfig.from_raw(raw(snapshot_ttl_seconds=1)), "snapshot_ttl_seconds", 1
    )


def test_min_substring_query_length_at_one_accepted():
    _assert_enabled_with(
        ClickConfig.from_raw(raw(min_substring_query_length=1)),
        "min_substring_query_length",
        1,
    )


# >=100 int: the exact minimum 100 is accepted.

def test_response_timeout_at_100_accepted():
    # Isolate the response_timeout_ms boundary: OMIT walk_deadline_ms so the
    # missing-key clamp floors it at 100 and keeps the config enabled (an
    # explicit walk_deadline_ms must be STRICTLY < response_timeout_ms, so an
    # explicit 100 alongside a 100 awaiter would disable on the walk bound).
    base = copy.deepcopy(VALID_RAW)
    base["response_timeout_ms"] = 100
    del base["walk_deadline_ms"]
    _assert_enabled_with(
        ClickConfig.from_raw(base),
        "response_timeout_ms",
        100,
    )


# ---------------------------------------------------------------------------
# (c) bool key given a non-bool -> disabled.
# ---------------------------------------------------------------------------

def test_enabled_non_bool_disables():
    # enabled itself given a non-bool is a validation failure (not degrade-by-
    # user): invalid_key names the key.
    _assert_disabled(ClickConfig.from_raw(raw(enabled=1)), "enabled")


def test_use_focus_targeting_non_bool_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(use_focus_targeting="yes")), "use_focus_targeting"
    )


def test_enable_offmonitor_fallback_non_bool_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(enable_offmonitor_fallback=0)),
        "enable_offmonitor_fallback",
    )


def test_enable_screen_reader_flag_non_bool_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(enable_screen_reader_flag=1)),
        "enable_screen_reader_flag",
    )


def test_enable_coordinate_click_on_com_error_non_bool_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(enable_coordinate_click_on_com_error="true")),
        "enable_coordinate_click_on_com_error",
    )


# ---------------------------------------------------------------------------
# (d) int key given a bool or float -> disabled (bool-is-int trap).
# ---------------------------------------------------------------------------

def test_int_key_given_bool_disables():
    # True would pass an >=1 range check if accepted as 1; it must be rejected.
    _assert_disabled(
        ClickConfig.from_raw(raw(notice_max_names=True)), "notice_max_names"
    )


def test_int_key_given_float_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(snapshot_ttl_seconds=30.0)), "snapshot_ttl_seconds"
    )


def test_tiebreaker_int_given_bool_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(tiebreaker_influence_logical_px=False)),
        "tiebreaker_influence_logical_px",
    )


def test_float_key_given_bool_disables():
    # bool must not satisfy a float key either.
    _assert_disabled(ClickConfig.from_raw(raw(min_confidence=True)), "min_confidence")


def test_float_key_accepts_int():
    # A real int is a valid float (promoted): 1 is in [0.0, 1.0].
    cfg = ClickConfig.from_raw(raw(min_confidence=1))
    assert cfg.enabled is True
    assert cfg.min_confidence == 1.0
    assert isinstance(cfg.min_confidence, float)


def test_int_key_given_string_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(response_timeout_ms="3000")), "response_timeout_ms"
    )


def test_float_key_given_string_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(min_confidence="0.4")), "min_confidence"
    )


# ---------------------------------------------------------------------------
# (e) browser_processes / browser_processes_extend list rules.
# ---------------------------------------------------------------------------

def test_browser_processes_non_list_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(browser_processes="brave.exe")), "browser_processes"
    )


def test_browser_processes_extend_non_list_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(browser_processes_extend={"a.exe"})),
        "browser_processes_extend",
    )


def test_browser_processes_entry_not_exe_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(browser_processes=["brave.exe", "notabrowser"])),
        "browser_processes",
    )


def test_browser_processes_extend_entry_not_exe_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(browser_processes_extend=["bad"])),
        "browser_processes_extend",
    )


def test_browser_processes_non_string_entry_disables():
    _assert_disabled(
        ClickConfig.from_raw(raw(browser_processes=["brave.exe", 123])),
        "browser_processes",
    )


def test_browser_processes_empty_list_is_valid():
    cfg = ClickConfig.from_raw(raw(browser_processes=[]))
    assert cfg.enabled is True
    assert cfg.browser_processes == ()


def test_browser_processes_case_variant_exe_is_accepted():
    # Windows executable names are case-insensitive; an upper-case ".EXE" suffix
    # is legitimate user input and must NOT disable the whole feature (wh-9f3t.30.2).
    cfg = ClickConfig.from_raw(raw(browser_processes=["Chrome.EXE"]))
    assert cfg.enabled is True
    assert cfg.invalid_key is None
    assert cfg.browser_processes == ("Chrome.EXE",)


def test_browser_processes_extend_case_variant_exe_is_accepted():
    cfg = ClickConfig.from_raw(raw(browser_processes_extend=["BRAVE.EXE"]))
    assert cfg.enabled is True
    assert cfg.invalid_key is None
    assert cfg.browser_processes_extend == ("BRAVE.EXE",)


# ---------------------------------------------------------------------------
# (f) Missing keys -> use the v5 default (documented policy).
# ---------------------------------------------------------------------------

def test_missing_key_uses_default():
    base = raw()
    del base["min_confidence"]
    cfg = ClickConfig.from_raw(base)
    assert cfg.enabled is True
    assert cfg.invalid_key is None
    assert cfg.min_confidence == 0.4  # the v5 default


def test_empty_dict_uses_all_defaults():
    cfg = ClickConfig.from_raw({})
    assert cfg.enabled is True
    assert cfg.invalid_key is None
    assert cfg.min_confidence == 0.4
    assert cfg.notice_max_names == 3
    assert cfg.response_timeout_ms == 3000
    assert cfg.browser_processes_extend == ()
    # browser_processes default is the full v5 starter list.
    assert "chrome.exe" in cfg.browser_processes


def test_missing_list_key_uses_default():
    base = raw()
    del base["browser_processes_extend"]
    cfg = ClickConfig.from_raw(base)
    assert cfg.enabled is True
    assert cfg.browser_processes_extend == ()


# ---------------------------------------------------------------------------
# (g) Degrade-by-user: valid enabled=false -> enabled=False, invalid_key None.
# ---------------------------------------------------------------------------

def test_user_disabled_is_not_a_validation_failure():
    cfg = ClickConfig.from_raw(raw(enabled=False))
    assert cfg.enabled is False
    assert cfg.invalid_key is None
    # The other valid values still round-trip.
    assert cfg.min_confidence == 0.4
    assert cfg.notice_max_names == 3


def test_user_disabled_with_a_bad_other_key_still_reports_validation_failure():
    # enabled=false (valid) but another key is bad: the bad key still wins so a
    # downstream surface can name it. invalid_key is NOT None here.
    cfg = ClickConfig.from_raw(raw(enabled=False, min_confidence=5.0))
    assert cfg.enabled is False
    assert cfg.invalid_key == "min_confidence"


# ---------------------------------------------------------------------------
# (h) NEVER raises, even on a wildly malformed dict.
# ---------------------------------------------------------------------------

def test_none_values_do_not_raise():
    cfg = ClickConfig.from_raw(raw(min_confidence=None))
    assert cfg.enabled is False
    assert cfg.invalid_key == "min_confidence"


def test_wildly_malformed_dict_returns_disabled_without_raising():
    junk: dict[str, Any] = {
        "enabled": object(),
        "min_confidence": [1, 2, 3],
        "browser_processes": 42,
        "notice_max_names": None,
        "snapshot_ttl_seconds": {"nested": "garbage"},
    }
    cfg = ClickConfig.from_raw(junk)  # must not raise
    assert cfg.enabled is False
    assert cfg.invalid_key is not None


def test_nested_wrong_types_in_list_do_not_raise():
    cfg = ClickConfig.from_raw(raw(browser_processes=[None, {"x": 1}]))
    assert cfg.enabled is False
    assert cfg.invalid_key == "browser_processes"


def test_completely_empty_call_path_never_raises():
    # Belt-and-braces: an empty dict is the most-degenerate valid input and must
    # produce an ENABLED config (all defaults), proving the never-raise contract
    # does not accidentally disable a valid empty block.
    cfg = ClickConfig.from_raw({})
    assert cfg.enabled is True


# ---------------------------------------------------------------------------
# (h2) Non-dict raw -> disabled (wh-9f3t.30.1). A malformed [click] scalar/list
# must DISABLE the feature, not silently enable a defaults config.
# ---------------------------------------------------------------------------

def test_list_raw_disables_without_raising():
    cfg = ClickConfig.from_raw([])  # type: ignore[arg-type]
    assert cfg.enabled is False
    assert cfg.invalid_key is not None


def test_string_raw_disables_without_raising():
    cfg = ClickConfig.from_raw("foo")  # type: ignore[arg-type]
    assert cfg.enabled is False
    assert cfg.invalid_key is not None


def test_tuple_raw_disables_without_raising():
    cfg = ClickConfig.from_raw(())  # type: ignore[arg-type]
    assert cfg.enabled is False
    assert cfg.invalid_key is not None


def test_none_raw_disables_without_raising():
    # None already disables today (the catch-all wrapper); assert it stays
    # disabled and does not raise.
    cfg = ClickConfig.from_raw(None)  # type: ignore[arg-type]
    assert cfg.enabled is False
    assert cfg.invalid_key is not None


# ---------------------------------------------------------------------------
# (i) DISABLED_CLICK_CONFIG sentinel.
# ---------------------------------------------------------------------------

def test_disabled_sentinel_is_disabled():
    assert DISABLED_CLICK_CONFIG.enabled is False


def test_disabled_sentinel_is_a_click_config():
    assert isinstance(DISABLED_CLICK_CONFIG, ClickConfig)


# ---------------------------------------------------------------------------
# (j) Phase 1.5 overlay keys (wh-n29v.29).
#
# Ten overlay fields validate on a SEPARATE track from the Phase 1 keys: a bad
# overlay value sets overlay_enabled_effective=False and adds the offending
# key to the NEW overlay_invalid_key tuple -- it does NOT set Phase 1's
# invalid_key string and does NOT set enabled=False, so by-name click stays
# operative. overlay_enabled_effective == (overlay_enabled AND
# overlay_invalid_key == ()). Missing overlay key -> its default; defaults alone
# never populate overlay_invalid_key. Ranges (design-v4 Configuration):
#   overlay_enabled                     bool   default True
#   overlay_badge_font_pt               int    [6, 96]      default 16
#   overlay_badge_shadow                bool   default True
#   overlay_auto_open_on_ambiguous      bool   default True
#   overlay_focus_debounce_ms           int    [0, 5000]    default 250
#   overlay_bounds_tolerance_physical_px int    [0, 200]     default 8
#   snapshot_store_capacity             int    [1, 64]      default 4
#   overlay_browser_refresh_seconds     int    [0, 300]     default 10 (0=off)
# ---------------------------------------------------------------------------

OVERLAY_DEFAULTS: dict[str, Any] = {
    "overlay_enabled": True,
    "overlay_badge_font_pt": 16,
    "overlay_badge_shadow": True,
    "overlay_auto_open_on_ambiguous": True,
    "overlay_focus_debounce_ms": 250,
    "overlay_bounds_tolerance_physical_px": 8,
    "snapshot_store_capacity": 4,
    "overlay_browser_refresh_seconds": 10,
    "overlay_badge_corner": "top_right",
    "overlay_badge_trailing_space": True,
}


def _assert_overlay_disabled(cfg: ClickConfig, key: str) -> None:
    """A bad overlay key disables ONLY the overlay; by-name click stays on."""
    assert cfg.enabled is True
    assert cfg.invalid_key is None
    assert cfg.overlay_enabled_effective is False
    assert key in cfg.overlay_invalid_key


# -- defaults round-trip when present at their design-v4 values --------------

def test_overlay_keys_round_trip_at_defaults():
    cfg = ClickConfig.from_raw(raw(**OVERLAY_DEFAULTS))
    assert cfg.enabled is True
    assert cfg.invalid_key is None
    assert cfg.overlay_enabled is True
    assert cfg.overlay_badge_font_pt == 16
    assert cfg.overlay_badge_shadow is True
    assert cfg.overlay_auto_open_on_ambiguous is True
    assert cfg.overlay_focus_debounce_ms == 250
    assert cfg.overlay_bounds_tolerance_physical_px == 8
    assert cfg.snapshot_store_capacity == 4
    assert cfg.overlay_browser_refresh_seconds == 10
    assert cfg.overlay_badge_corner == "top_right"
    assert cfg.overlay_badge_trailing_space is True
    assert cfg.overlay_invalid_key == ()
    assert cfg.overlay_enabled_effective is True


def test_overlay_keys_default_when_absent():
    # The Phase 1 VALID_RAW has no overlay keys; absent overlay keys take their
    # defaults and do NOT populate overlay_invalid_key.
    cfg = ClickConfig.from_raw(raw())
    assert cfg.enabled is True
    assert cfg.overlay_enabled is True
    assert cfg.overlay_badge_font_pt == 16
    assert cfg.overlay_badge_shadow is True
    assert cfg.overlay_auto_open_on_ambiguous is True
    assert cfg.overlay_focus_debounce_ms == 250
    assert cfg.overlay_bounds_tolerance_physical_px == 8
    assert cfg.snapshot_store_capacity == 4
    assert cfg.overlay_browser_refresh_seconds == 10
    assert cfg.overlay_badge_trailing_space is True
    assert cfg.overlay_invalid_key == ()
    assert cfg.overlay_enabled_effective is True


def test_empty_dict_sets_overlay_defaults():
    cfg = ClickConfig.from_raw({})
    assert cfg.overlay_enabled is True
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_invalid_key == ()
    assert cfg.overlay_focus_debounce_ms == 250
    assert cfg.snapshot_store_capacity == 4


# -- overlay_enabled (bool) --------------------------------------------------

def test_overlay_enabled_false_clears_effective_but_not_invalid():
    # A valid overlay_enabled=false is an operator opt-out, NOT a fault: the
    # effective flag is False but overlay_invalid_key stays empty and Phase 1
    # invalid_key stays None.
    cfg = ClickConfig.from_raw(raw(overlay_enabled=False))
    assert cfg.enabled is True
    assert cfg.invalid_key is None
    assert cfg.overlay_enabled is False
    assert cfg.overlay_enabled_effective is False
    assert cfg.overlay_invalid_key == ()


def test_overlay_enabled_non_bool_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_enabled=1)), "overlay_enabled"
    )


def test_overlay_badge_shadow_non_bool_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_badge_shadow="yes")), "overlay_badge_shadow"
    )


def test_overlay_auto_open_non_bool_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_auto_open_on_ambiguous=0)),
        "overlay_auto_open_on_ambiguous",
    )


# -- overlay_badge_font_pt int [6, 96] ---------------------------------------

def test_overlay_badge_font_pt_at_lower_bound_accepted():
    cfg = ClickConfig.from_raw(raw(overlay_badge_font_pt=6))
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_badge_font_pt == 6


def test_overlay_badge_font_pt_at_upper_bound_accepted():
    cfg = ClickConfig.from_raw(raw(overlay_badge_font_pt=96))
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_badge_font_pt == 96


def test_overlay_badge_font_pt_below_lower_bound_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_badge_font_pt=5)), "overlay_badge_font_pt"
    )


def test_overlay_badge_font_pt_above_upper_bound_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_badge_font_pt=97)), "overlay_badge_font_pt"
    )


def test_overlay_badge_font_pt_bool_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_badge_font_pt=True)), "overlay_badge_font_pt"
    )


def test_overlay_badge_font_pt_float_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_badge_font_pt=16.0)), "overlay_badge_font_pt"
    )


# -- overlay_badge_corner str in {top_left,top_right,bottom_left,bottom_right} -
# Default top_right so the digit clears the icon and label start on left-aligned
# list/tree rows (wh-overlay-badge-occludes-label follow-up).

def test_overlay_badge_corner_default_is_top_right():
    cfg = ClickConfig.from_raw(raw())
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_badge_corner == "top_right"


def test_overlay_badge_corner_each_valid_value_accepted():
    for corner in ("top_left", "top_right", "bottom_left", "bottom_right"):
        cfg = ClickConfig.from_raw(raw(overlay_badge_corner=corner))
        assert cfg.overlay_enabled_effective is True, corner
        assert cfg.overlay_badge_corner == corner


def test_overlay_badge_corner_unknown_string_disables_overlay_only():
    cfg = ClickConfig.from_raw(raw(overlay_badge_corner="middle"))
    _assert_overlay_disabled(cfg, "overlay_badge_corner")
    # A bad value keeps the field at its default, not the rejected string.
    assert cfg.overlay_badge_corner == "top_right"


def test_overlay_badge_corner_non_string_disables_overlay_only():
    cfg = ClickConfig.from_raw(raw(overlay_badge_corner=1))
    _assert_overlay_disabled(cfg, "overlay_badge_corner")
    assert cfg.overlay_badge_corner == "top_right"


# -- overlay_badge_trailing_space (bool) -------------------------------------
# Default True: the number is placed in the empty space just past the control's
# trailing edge when that strip is clear, else it falls back to the corner
# (wh-overlay-badge-occludes-label follow-up). False restores pure corner
# placement.

def test_overlay_badge_trailing_space_default_is_true():
    cfg = ClickConfig.from_raw(raw())
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_badge_trailing_space is True


def test_overlay_badge_trailing_space_false_accepted():
    cfg = ClickConfig.from_raw(raw(overlay_badge_trailing_space=False))
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_badge_trailing_space is False


def test_overlay_badge_trailing_space_non_bool_disables_overlay_only():
    cfg = ClickConfig.from_raw(raw(overlay_badge_trailing_space="yes"))
    _assert_overlay_disabled(cfg, "overlay_badge_trailing_space")
    # A bad value keeps the field at its default, not the rejected value.
    assert cfg.overlay_badge_trailing_space is True


# -- overlay_focus_debounce_ms int [0, 5000]; 0 IS valid (no-debounce) -------

def test_overlay_focus_debounce_ms_zero_accepted():
    # Range starts at 0: a configured 0 means "no debounce" and MUST be accepted
    # (not clamped, not flagged). The Logic process builds its focus-change
    # debouncer directly from this validated value (wh-n29v.66), so a configured
    # 0 reaches FocusChangeDebouncer as a no-debounce window (wh-n29v.21
    # reviewer_0 24.1).
    cfg = ClickConfig.from_raw(raw(overlay_focus_debounce_ms=0))
    assert cfg.enabled is True
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_invalid_key == ()
    assert cfg.overlay_focus_debounce_ms == 0


def test_overlay_focus_debounce_ms_at_upper_bound_accepted():
    cfg = ClickConfig.from_raw(raw(overlay_focus_debounce_ms=5000))
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_focus_debounce_ms == 5000


def test_overlay_focus_debounce_ms_negative_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_focus_debounce_ms=-1)),
        "overlay_focus_debounce_ms",
    )


def test_overlay_focus_debounce_ms_above_upper_bound_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_focus_debounce_ms=5001)),
        "overlay_focus_debounce_ms",
    )


def test_overlay_focus_debounce_ms_bool_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_focus_debounce_ms=False)),
        "overlay_focus_debounce_ms",
    )


# -- overlay_bounds_tolerance_physical_px int [0, 200] ------------------------

def test_overlay_bounds_tolerance_at_lower_bound_accepted():
    cfg = ClickConfig.from_raw(raw(overlay_bounds_tolerance_physical_px=0))
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_bounds_tolerance_physical_px == 0


def test_overlay_bounds_tolerance_at_upper_bound_accepted():
    cfg = ClickConfig.from_raw(raw(overlay_bounds_tolerance_physical_px=200))
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_bounds_tolerance_physical_px == 200


def test_overlay_bounds_tolerance_negative_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_bounds_tolerance_physical_px=-1)),
        "overlay_bounds_tolerance_physical_px",
    )


def test_overlay_bounds_tolerance_above_upper_bound_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_bounds_tolerance_physical_px=201)),
        "overlay_bounds_tolerance_physical_px",
    )


def test_overlay_bounds_tolerance_bool_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_bounds_tolerance_physical_px=True)),
        "overlay_bounds_tolerance_physical_px",
    )


# -- overlay_bounds_tolerance_physical_px rename + deprecated alias ----------
# wh-bounds-tol-rename-physical: the tolerance is compared in PHYSICAL UIA
# pixels (both the walk-time bounds and the pre-click re-read are physical),
# so the key and field are renamed *_logical_px -> *_physical_px. The old key
# stays accepted as a deprecated alias so an existing user config.toml keeps
# working; a deprecation warning names both keys.


def test_bounds_tolerance_physical_px_key_accepted():
    cfg = ClickConfig.from_raw(raw(overlay_bounds_tolerance_physical_px=12))
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_bounds_tolerance_physical_px == 12


def test_bounds_tolerance_old_logical_key_is_deprecated_alias(caplog):
    with caplog.at_level(logging.WARNING):
        cfg = ClickConfig.from_raw(raw(overlay_bounds_tolerance_logical_px=12))
    assert cfg.overlay_bounds_tolerance_physical_px == 12
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_invalid_key == ()
    assert any(
        "overlay_bounds_tolerance_logical_px" in rec.getMessage()
        and "overlay_bounds_tolerance_physical_px" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    )


def test_bounds_tolerance_new_key_wins_when_both_present():
    cfg = ClickConfig.from_raw(
        raw(
            overlay_bounds_tolerance_physical_px=12,
            overlay_bounds_tolerance_logical_px=99,
        )
    )
    assert cfg.overlay_bounds_tolerance_physical_px == 12
    assert cfg.overlay_enabled_effective is True


def test_bounds_tolerance_bad_old_key_records_old_name():
    # The operator's log must name the key the user actually wrote (the old
    # alias), not the canonical name absent from their file.
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_bounds_tolerance_logical_px=-1)),
        "overlay_bounds_tolerance_logical_px",
    )


# -- overlay_browser_refresh_seconds int [0, 300] -----------------------------
# wh-n29v.121: the proactive-refresh trust window for a painted overlay over a
# browser window. 0 is a VALID value meaning "never proactively refresh"
# (opt-out), so the floor is 0, not 1.


def test_overlay_browser_refresh_seconds_valid_value_round_trips():
    cfg = ClickConfig.from_raw(raw(overlay_browser_refresh_seconds=30))
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_browser_refresh_seconds == 30


def test_overlay_browser_refresh_seconds_zero_accepted():
    cfg = ClickConfig.from_raw(raw(overlay_browser_refresh_seconds=0))
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_browser_refresh_seconds == 0


def test_overlay_browser_refresh_seconds_at_upper_bound_accepted():
    cfg = ClickConfig.from_raw(raw(overlay_browser_refresh_seconds=300))
    assert cfg.overlay_enabled_effective is True
    assert cfg.overlay_browser_refresh_seconds == 300


def test_overlay_browser_refresh_seconds_negative_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_browser_refresh_seconds=-1)),
        "overlay_browser_refresh_seconds",
    )


def test_overlay_browser_refresh_seconds_above_upper_bound_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_browser_refresh_seconds=301)),
        "overlay_browser_refresh_seconds",
    )


def test_overlay_browser_refresh_seconds_bool_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(overlay_browser_refresh_seconds=True)),
        "overlay_browser_refresh_seconds",
    )


# -- snapshot_store_capacity int [1, 64] -------------------------------------

def test_snapshot_store_capacity_at_lower_bound_accepted():
    cfg = ClickConfig.from_raw(raw(snapshot_store_capacity=1))
    assert cfg.overlay_enabled_effective is True
    assert cfg.snapshot_store_capacity == 1


def test_snapshot_store_capacity_at_upper_bound_accepted():
    cfg = ClickConfig.from_raw(raw(snapshot_store_capacity=64))
    assert cfg.overlay_enabled_effective is True
    assert cfg.snapshot_store_capacity == 64


def test_snapshot_store_capacity_below_lower_bound_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(snapshot_store_capacity=0)),
        "snapshot_store_capacity",
    )


def test_snapshot_store_capacity_above_upper_bound_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(snapshot_store_capacity=65)),
        "snapshot_store_capacity",
    )


def test_snapshot_store_capacity_bool_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(snapshot_store_capacity=True)),
        "snapshot_store_capacity",
    )


def test_snapshot_store_capacity_float_disables_overlay_only():
    _assert_overlay_disabled(
        ClickConfig.from_raw(raw(snapshot_store_capacity=4.0)),
        "snapshot_store_capacity",
    )


# -- cross-cutting overlay invariants ----------------------------------------

def test_multiple_bad_overlay_keys_all_listed():
    cfg = ClickConfig.from_raw(
        raw(overlay_badge_font_pt=1000, snapshot_store_capacity=0)
    )
    assert cfg.enabled is True
    assert cfg.invalid_key is None
    assert cfg.overlay_enabled_effective is False
    assert "overlay_badge_font_pt" in cfg.overlay_invalid_key
    assert "snapshot_store_capacity" in cfg.overlay_invalid_key
    assert len(cfg.overlay_invalid_key) == 2


def test_bad_overlay_key_does_not_disable_by_name_click():
    # The headline invariant: a bad overlay value leaves Phase 1 fully enabled.
    cfg = ClickConfig.from_raw(raw(overlay_focus_debounce_ms=-1))
    assert cfg.enabled is True
    assert cfg.invalid_key is None
    assert cfg.min_confidence == 0.4  # Phase 1 keys still validated normally
    assert cfg.overlay_enabled_effective is False
    assert cfg.overlay_invalid_key == ("overlay_focus_debounce_ms",)


def test_effective_equals_enabled_and_empty_invalid_list():
    # overlay_enabled_effective == (overlay_enabled AND overlay_invalid_key == ()).
    for raw_kwargs, exp_effective in (
        ({}, True),
        ({"overlay_enabled": False}, False),
        ({"snapshot_store_capacity": 0}, False),
        ({"overlay_enabled": False, "snapshot_store_capacity": 0}, False),
    ):
        cfg = ClickConfig.from_raw(raw(**raw_kwargs))
        expected = cfg.overlay_enabled and cfg.overlay_invalid_key == ()
        assert cfg.overlay_enabled_effective is exp_effective
        assert cfg.overlay_enabled_effective is expected


def test_phase1_invalid_key_disables_whole_feature_including_overlay():
    # A bad Phase 1 key disables the whole feature (regression preserved); when
    # the feature is hard-disabled the overlay is also effectively off.
    cfg = ClickConfig.from_raw(raw(min_confidence=5.0))
    assert cfg.enabled is False
    assert cfg.invalid_key == "min_confidence"
    assert cfg.overlay_enabled_effective is False
    # wh-n29v.32.3 gap 3: lock in that the hard-disable path carries a fresh
    # empty overlay_invalid_key (no overlay key was the cause of the disable).
    assert cfg.overlay_invalid_key == ()


def test_overlay_keys_not_shared_across_instances():
    # overlay_invalid_key carries each instance's own bad-key set. The field is an
    # immutable tuple, so there is no shared-mutable-default hazard at all; this
    # still confirms two configs hold independent values, not one shared object.
    cfg_a = ClickConfig.from_raw(raw(snapshot_store_capacity=0))
    cfg_b = ClickConfig.from_raw(raw(overlay_badge_font_pt=1000))
    assert cfg_a.overlay_invalid_key == ("snapshot_store_capacity",)
    assert cfg_b.overlay_invalid_key == ("overlay_badge_font_pt",)
    assert cfg_a.overlay_invalid_key != cfg_b.overlay_invalid_key


# -- _disabled / DISABLED_CLICK_CONFIG carry overlay defaults -----------------

def test_disabled_sentinel_overlay_defaults():
    assert DISABLED_CLICK_CONFIG.overlay_enabled_effective is False
    assert DISABLED_CLICK_CONFIG.overlay_invalid_key == ()


def test_phase1_disabled_config_carries_overlay_defaults():
    # A Phase 1 validation failure goes through _disabled(...); it must carry
    # overlay_enabled_effective=False and a fresh empty overlay_invalid_key.
    cfg = ClickConfig.from_raw(raw(min_confidence=5.0))
    assert cfg.enabled is False
    assert cfg.overlay_enabled_effective is False
    assert cfg.overlay_invalid_key == ()


def test_non_table_raw_carries_overlay_defaults():
    cfg = ClickConfig.from_raw("foo")  # type: ignore[arg-type]
    assert cfg.enabled is False
    assert cfg.overlay_enabled_effective is False
    assert cfg.overlay_invalid_key == ()


def test_disabled_paths_satisfy_effective_invariant():
    # The invariant overlay_enabled_effective == (overlay_enabled AND
    # overlay_invalid_key == ()) must hold on the _disabled() paths too, not just
    # the validated path. _disabled sets overlay_enabled=False so the formula
    # holds in every case (wh-n29v.31.1). Covers the sentinel, a Phase 1
    # validation failure, and a non-table raw. (The operator enabled=false
    # opt-out goes through the validated path, not _disabled, so it is covered by
    # test_effective_equals_enabled_and_empty_invalid_list instead.)
    disabled_configs = [
        DISABLED_CLICK_CONFIG,
        ClickConfig.from_raw(raw(min_confidence=5.0)),  # Phase 1 failure -> _disabled
        ClickConfig.from_raw("not-a-table"),  # type: ignore[arg-type]
    ]
    for cfg in disabled_configs:
        assert cfg.enabled is False
        assert cfg.overlay_enabled is False
        assert cfg.overlay_invalid_key == ()
        assert cfg.overlay_enabled_effective is False
        assert cfg.overlay_enabled_effective == (
            cfg.overlay_enabled and cfg.overlay_invalid_key == ()
        )


def test_overlay_invalid_key_field_is_a_tuple():
    # The field is an immutable tuple (not a list), so a frozen ClickConfig stays
    # hashable and the bad-key set cannot be mutated in place (wh-n29v.30.2).
    cfg = ClickConfig.from_raw(raw())
    assert isinstance(cfg.overlay_invalid_key, tuple)
    cfg_bad = ClickConfig.from_raw(raw(snapshot_store_capacity=0))
    assert isinstance(cfg_bad.overlay_invalid_key, tuple)


# -- two-process determinism (wh-n29v.30.1) ----------------------------------
# Logic and Input each run from_raw over the SAME raw [click] block with no IPC
# handoff, so the two processes MUST derive identical configs -- including the
# ORDER of overlay_invalid_key -- from identical input. from_raw iterates the
# fixed _OVERLAY_VALIDATORS dict (not the raw dict), which makes the result
# deterministic and independent of the raw key order. These tests lock that in
# so a future switch to iterating ``raw`` cannot silently desync the two
# processes' overlay_invalid_key order.

def test_from_raw_is_deterministic_for_identical_input():
    r = raw(
        overlay_badge_font_pt=1000,
        overlay_bounds_tolerance_physical_px=-1,
        snapshot_store_capacity=0,
    )
    first = ClickConfig.from_raw(r)
    second = ClickConfig.from_raw(r)
    assert first == second
    assert first.overlay_invalid_key == second.overlay_invalid_key


def test_overlay_invalid_key_order_independent_of_raw_key_order():
    # Same three bad overlay keys, inserted into the raw dict in two different
    # orders. The output overlay_invalid_key order follows _OVERLAY_VALIDATORS,
    # not the raw insertion order, so both inputs yield the SAME tuple in the
    # SAME order -- the property the two processes rely on to agree.
    base = raw()
    bad = {
        "overlay_badge_font_pt": 1000,
        "overlay_bounds_tolerance_physical_px": -1,
        "snapshot_store_capacity": 0,
    }
    order_a = {**base, **bad}
    order_b = {**base}
    for key in (
        "snapshot_store_capacity",
        "overlay_bounds_tolerance_physical_px",
        "overlay_badge_font_pt",
    ):
        order_b[key] = bad[key]
    cfg_a = ClickConfig.from_raw(order_a)
    cfg_b = ClickConfig.from_raw(order_b)
    assert cfg_a.overlay_invalid_key == cfg_b.overlay_invalid_key
    assert cfg_a.overlay_invalid_key == (
        "overlay_badge_font_pt",
        "overlay_bounds_tolerance_physical_px",
        "snapshot_store_capacity",
    )


def test_click_config_is_hashable():
    # A frozen dataclass auto-generates __hash__ over its fields; the
    # overlay_invalid_key tuple keeps every ClickConfig hashable, including a
    # config whose overlay validation failed and the disabled sentinel
    # (wh-n29v.30.2). A list field would raise TypeError on hash().
    cfg_default = ClickConfig.from_raw(raw())
    cfg_bad_overlay = ClickConfig.from_raw(raw(snapshot_store_capacity=0))
    cfg_disabled = ClickConfig.from_raw(raw(min_confidence=5.0))
    assert isinstance(hash(cfg_default), int)
    assert isinstance(hash(cfg_bad_overlay), int)
    assert isinstance(hash(cfg_disabled), int)
    assert isinstance(hash(DISABLED_CLICK_CONFIG), int)
    # Equal configs hash equally, and a ClickConfig is usable as a set/dict key.
    assert hash(cfg_default) == hash(ClickConfig.from_raw(raw()))
    assert len({cfg_default, ClickConfig.from_raw(raw())}) == 1


def test_click_config_is_frozen():
    cfg = ClickConfig.from_raw(raw())
    try:
        cfg.enabled = True  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001
        assert exc.__class__.__name__ == "FrozenInstanceError"
    else:
        raise AssertionError("ClickConfig should be frozen")


# -- overlay_enabled_effective is derived, not stored (wh-n29v.32.1, .32.2) ---
# The effective flag is a computed property, not a stored field with a default.
# Deriving it removes the only way the documented invariant
# (overlay_enabled_effective == (overlay_enabled AND overlay_invalid_key == ()))
# could be violated: there is no separate field to fall out of sync, so neither
# a default value nor a dataclasses.replace can leave a stale flag behind.

def test_overlay_enabled_effective_is_not_a_constructor_field():
    # The flag is a read-only derived property, so it is NOT an __init__ field
    # and cannot be set to an inconsistent value at construction time. This is
    # the structural fix for wh-n29v.32.1 (no inconsistent default is possible
    # because there is no field default) and wh-n29v.32.2 (no stored field to
    # enforce, because nothing stores it).
    field_names = {f.name for f in dataclasses.fields(ClickConfig)}
    assert "overlay_enabled_effective" not in field_names
    assert "overlay_enabled" in field_names
    assert "overlay_invalid_key" in field_names


def test_overlay_enabled_effective_recomputes_under_replace():
    # dataclasses.replace is the "make a modified copy" pattern for frozen
    # dataclasses. With a stored effective field it would carry the old value
    # forward and desync; as a derived property it always recomputes from the
    # current overlay_enabled and overlay_invalid_key (wh-n29v.32.2).
    cfg = ClickConfig.from_raw(raw())
    assert cfg.overlay_enabled is True
    assert cfg.overlay_invalid_key == ()
    assert cfg.overlay_enabled_effective is True
    # Flipping overlay_enabled off must flip the derived flag off.
    flipped = dataclasses.replace(cfg, overlay_enabled=False)
    assert flipped.overlay_enabled_effective is False
    # A non-empty overlay_invalid_key must also flip it off, even with
    # overlay_enabled still True.
    with_bad = dataclasses.replace(
        cfg, overlay_invalid_key=("snapshot_store_capacity",)
    )
    assert with_bad.overlay_enabled is True
    assert with_bad.overlay_enabled_effective is False
    # Restoring consistent inputs restores the derived flag.
    restored = dataclasses.replace(flipped, overlay_enabled=True)
    assert restored.overlay_enabled_effective is True


def test_overlay_invalid_key_populated_when_enabled_false_and_key_bad():
    # wh-n29v.32.3 gap 1: when the operator set overlay_enabled=false AND another
    # overlay key is bad, the bad key must STILL be recorded in
    # overlay_invalid_key (the operator-facing diagnostic), not silently dropped
    # because the effective flag is already False from the opt-out.
    cfg = ClickConfig.from_raw(
        raw(overlay_enabled=False, snapshot_store_capacity=0)
    )
    assert cfg.overlay_enabled_effective is False
    assert "snapshot_store_capacity" in cfg.overlay_invalid_key


def test_bad_overlay_enabled_and_another_bad_key_both_collected():
    # wh-n29v.32.3 gap 2: overlay_enabled itself being a bad (non-bool) value
    # alongside another bad overlay key -- both must land in overlay_invalid_key.
    # A future change to _validate_overlay that special-cased the first bad key
    # would drop the second; this pins that both are collected.
    cfg = ClickConfig.from_raw(raw(overlay_enabled=1, snapshot_store_capacity=0))
    assert cfg.overlay_enabled_effective is False
    assert "overlay_enabled" in cfg.overlay_invalid_key
    assert "snapshot_store_capacity" in cfg.overlay_invalid_key
    assert len(cfg.overlay_invalid_key) == 2
