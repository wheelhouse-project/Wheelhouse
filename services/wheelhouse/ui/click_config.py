"""ClickConfig feature-init validator for voice clicking (wh-1yqgn).

The voice element-clicking feature (epic wh-l4h.1) reads its settings from the
``[click]`` block of ``config.toml``. This module is the feature-init validator
that turns the raw, unchecked dict that ``ConfigService`` hands back into a
typed, range-checked :class:`ClickConfig`.

Why a feature-init helper and not ``ConfigService.load_config``:
================================================================
``ConfigService`` is a raw ``tomllib.load`` wrapper -- it raises on file-level
failures and otherwise returns an unchecked dict, with no per-feature degrade
path. Folding click-config validation into it would mean a single bad ``[click]``
value either crashes WheelHouse startup (taking down dictation, hotkeys, and
every plugin) or is silently accepted. So validation lives here, fail-soft.

The never-raises contract (the reason this slice exists):
=========================================================
``ClickConfig.from_raw`` NEVER raises. On the first type/range failure it logs
``logger.error`` naming the offending key and its raw value, and returns a
DISABLED config (``enabled=False``) recording ``invalid_key=<key>`` so a
downstream surface (the deferred startup notice / action short-circuit, bead
wh-tab7j) can name the key. Out-of-range config can never crash startup; the
global feature gate is ``enabled``.

Two distinct disabled shapes:
* degrade-by-validation -- a present key has a bad type/range:
  ``enabled=False`` AND ``invalid_key=<key>``.
* degrade-by-user -- the operator set a valid ``enabled=false``:
  ``enabled=False`` AND ``invalid_key=None`` (nothing failed; this is opt-out,
  not a fault). If ``enabled=false`` AND another key is also bad, the bad key
  still wins (``invalid_key`` names it) so the failure is still surfaced.

Missing-key policy:
===================
A MISSING key falls back to its v5 default and does NOT disable the feature --
an absent key is config-author omission, not a malformed value. Only a PRESENT
key with a bad type/range disables. An empty ``[click]`` block therefore yields
a fully-default ENABLED config.

bool-is-int trap (Python: ``bool`` is a subclass of ``int``):
=============================================================
int-typed keys reject a ``bool`` (so ``true`` is not silently read as ``1``);
bool-typed keys reject a non-``bool`` int/float (so ``1`` is not read as
``true``); float-typed keys accept a real ``int`` (promoted to float) but reject
``bool``. The ``isinstance`` checks below exclude ``bool`` everywhere it matters.

This slice ships the validator + the ``[click]`` config block + the unit tests
only. The two Logic-side wiring surfaces (the startup notice and the
``click_element`` action short-circuit) are DEFERRED to bead wh-tab7j.

Phase 1.5 overlay keys (wh-n29v.29):
====================================
Ten overlay fields validate on a SEPARATE track from the Phase 1 keys. A bad
overlay value DISABLES ONLY THE OVERLAY: it adds the offending key name to the
NEW ``overlay_invalid_key`` tuple, which makes the derived
``overlay_enabled_effective`` property ``False``. It does NOT set Phase 1's
``invalid_key`` string and does NOT set ``enabled=False`` -- by-name click stays
operative. Conversely, a bad Phase 1 key still disables the whole feature (and
the overlay with it, because the disabled path sets ``overlay_enabled=False``,
which makes the derived ``overlay_enabled_effective`` ``False``). The effective
gate is the derived property ``overlay_enabled_effective == (overlay_enabled
AND overlay_invalid_key == ())``. A configured ``overlay_focus_debounce_ms`` of 0 is
a valid "no debounce" value (range starts at 0) and is accepted, not clamped.
Logic and Input each run ``from_raw`` over the same raw ``[click]`` block
independently and agree on the overlay gating with no IPC handoff. Ranges and
defaults come from docs/plans/2026-05-28-voice-element-clicking-phase-1-5-
design-v4.md Configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# The v5 default browser-process starter list (config.toml is the single source
# of truth for the on-disk block; this mirrors it so a missing key degrades to
# the same list rather than to an empty one).
_DEFAULT_BROWSER_PROCESSES: tuple[str, ...] = (
    "brave.exe",
    "chrome.exe",
    "msedge.exe",
    "vivaldi.exe",
    "slack.exe",
    "discord.exe",
    "code.exe",
    "ms-teams.exe",
    "Teams.exe",
    "spotify.exe",
    "notion.exe",
    "obsidian.exe",
    "ChatGPT.exe",
)


@dataclass(frozen=True)
class ClickConfig:
    """Validated, immutable voice-clicking configuration.

    The 17 Phase 1 [click] keys plus ``invalid_key`` (``None`` when the config is
    valid -- or when the operator validly set ``enabled=false`` -- or the name of
    the first key that failed type/range validation when the feature was disabled
    BY a validation failure), plus the nine Phase 1.5 overlay fields
    (wh-n29v.29) and the derived ``overlay_enabled_effective`` property. The
    overlay fields validate on a separate track. ``overlay_enabled_effective``
    is a read-only property (NOT a stored field) that gates every overlay entry
    point and equals ``overlay_enabled AND overlay_invalid_key == ()`` by
    construction -- deriving it instead of storing it makes that invariant
    impossible to violate (wh-n29v.32.1, wh-n29v.32.2). ``overlay_invalid_key``
    is the tuple of overlay keys that failed validation (an immutable tuple, not
    a list, so the frozen dataclass stays hashable -- a list field would make
    every ClickConfig unhashable and would be mutable in place despite
    ``frozen=True``) and is SEPARATE from Phase 1's ``invalid_key`` string.
    """

    enabled: bool
    use_focus_targeting: bool
    enable_offmonitor_fallback: bool
    min_confidence: float
    clear_winner_margin: float
    tiebreaker_min_separation_logical_px: int
    tiebreaker_influence_logical_px: int
    notice_max_names: int
    enable_screen_reader_flag: bool
    snapshot_ttl_seconds: int
    response_timeout_ms: int
    walk_deadline_ms: int
    min_substring_query_length: int
    min_substring_overlap_ratio: float
    enable_coordinate_click_on_com_error: bool
    browser_processes: tuple[str, ...]
    browser_processes_extend: tuple[str, ...]
    invalid_key: Optional[str] = None
    # -- Phase 1.5 overlay fields (wh-n29v.29) -------------------------------
    overlay_enabled: bool = True
    overlay_badge_font_pt: int = 16
    # Which corner of a control the overlay number sits on. Default "top_right"
    # so the digit clears the icon and the label text, which Windows list rows,
    # tree items, and menu entries keep at the LEFT (wh-overlay-badge-occludes-
    # label). One of top_left / top_right / bottom_left / bottom_right.
    overlay_badge_corner: str = "top_right"
    # When True (default) the number is placed in the empty space just PAST the
    # control's trailing edge (the horizontal side of the configured corner)
    # whenever that strip is clear of other walked controls and stays on-monitor;
    # otherwise it falls back to the corner. This clears the control's own label
    # entirely -- the corner still overlaps a list row's size column or a tree
    # item's label end (wh-overlay-badge-occludes-label). False restores pure
    # corner placement.
    # Known limitation: the "clear" test only considers the other NUMBERED
    # controls, so the number can still be drawn over an element that was not
    # numbered (a scrollbar, or a static text label next to a checkbox). This is
    # cosmetic only -- the overlay never receives mouse input, so it cannot block
    # a click -- and setting this False restores the corner placement if it
    # bothers a user (reviewed 2026-07-04, kept cosmetic: a real fix would need a
    # second UI-tree walk plus a new Input-to-GUI message for the obstacle
    # rectangles).
    overlay_badge_trailing_space: bool = True
    overlay_badge_shadow: bool = True
    overlay_auto_open_on_ambiguous: bool = True
    overlay_focus_debounce_ms: int = 250
    # Renamed from overlay_bounds_tolerance_logical_px (wh-bounds-tol-rename-
    # physical): the compared units are PHYSICAL UIA pixels on both sides of
    # the pre-click drift check. The old config key is accepted as a
    # deprecated alias (see _DEPRECATED_OVERLAY_ALIASES).
    overlay_bounds_tolerance_physical_px: int = 8
    snapshot_store_capacity: int = 4
    # wh-n29v.121: proactive-refresh trust window (seconds) for a painted
    # overlay over a browser/Electron window (the effective browser-process
    # list). 0 disables the proactive refresh. The cadence is quantized to
    # the keepalive tick (snapshot_ttl_seconds/2, 15s at defaults), so the
    # real window is this value rounded up to the next tick.
    overlay_browser_refresh_seconds: int = 10
    # An immutable tuple, not a list: a frozen dataclass auto-generates __hash__
    # over its fields, so a list field would make every ClickConfig unhashable (a
    # regression from Phase 1, whose fields were all hashable) and would be
    # mutable in place despite frozen=True. field(default_factory=tuple) gives the
    # empty-case default; populated tuples are built in _validate_overlay and on
    # the _disabled path. overlay_enabled_effective is DERIVED from this tuple and
    # overlay_enabled (the property below), so it is NOT a stored field.
    overlay_invalid_key: tuple[str, ...] = field(default_factory=tuple)

    @property
    def overlay_enabled_effective(self) -> bool:
        """Whether the numbered overlay is effectively on (derived, not stored).

        The overlay is effectively enabled only when the operator left it on AND
        no overlay key failed validation::

            overlay_enabled_effective == (overlay_enabled AND overlay_invalid_key == ())

        Deriving this from the two stored fields -- rather than storing a
        precomputed bool -- makes that invariant impossible to violate: there is
        no separate field to fall out of sync, so neither a field default nor a
        ``dataclasses.replace`` that changes ``overlay_enabled`` can leave a
        stale effective flag behind (wh-n29v.32.1, wh-n29v.32.2). Logic and Input
        each derive the same value from the same raw ``[click]`` block, so the
        two processes cannot disagree on whether the overlay is on.
        """
        return self.overlay_enabled and self.overlay_invalid_key == ()

    @classmethod
    def from_raw(cls, raw: Any) -> "ClickConfig":
        """Validate a raw ``[click]`` value; never raises.

        ``raw`` is typed ``Any`` deliberately: ``ConfigService`` returns
        ``Any`` and a malformed ``[click]`` value can be a non-table (a
        scalar or list). The ``isinstance(raw, dict)`` guard below is a real
        runtime branch, not dead code -- the annotation must not narrow it
        away.

        On the first present-key type/range failure, log an error naming the
        key and its raw value, and return a disabled config (``enabled=False``,
        ``invalid_key=<key>``). Missing keys use the v5 default. A valid
        ``enabled=false`` returns ``enabled=False`` with ``invalid_key=None``.
        """
        if not isinstance(raw, dict):
            # A non-dict raw (a list, str, tuple, None, ...) is a malformed
            # [click] value -- e.g. TOML ``click = "x"`` or ``click = []``, which
            # ConfigService hands through as a scalar/list, not a table. The
            # ``key not in raw`` missing-key check would silently short-circuit
            # every key to its default and return a fully-ENABLED config, which
            # violates the fail-soft contract. Disable explicitly (wh-9f3t.30.1).
            logger.error(
                "ClickConfig.from_raw received a non-table [click] value of type "
                "%s; disabling voice clicking. The [click] block must be a table.",
                type(raw).__name__,
            )
            return _disabled(invalid_key="<not-a-table>")
        try:
            return cls._validate(raw)
        except Exception:  # noqa: BLE001 -- the contract is to NEVER raise
            # A truly unexpected shape (e.g. ``raw`` is not a Mapping) must not
            # propagate. Disable with a sentinel invalid_key so a downstream
            # surface can still tell the operator something is wrong.
            logger.error(
                "ClickConfig.from_raw hit an unexpected error validating the "
                "[click] config block; disabling voice clicking.",
                exc_info=True,
            )
            return _disabled(invalid_key="<unparseable>")

    @classmethod
    def _validate(cls, raw: dict[str, Any]) -> "ClickConfig":
        # Each entry: (key, validator) -> returns the coerced value or raises
        # _BadKey(key) on a present-but-invalid value. A missing key short-
        # circuits to its default before the validator runs.
        defaults = _DEFAULTS

        def take(key: str, validate: Any) -> Any:
            if key not in raw:
                return defaults[key]
            value = raw[key]
            ok, coerced = validate(value)
            if not ok:
                raise _BadKey(key, value)
            return coerced

        # Validate every key. The FIRST bad key raises _BadKey, caught below, so
        # the offending key is named even when several are bad.
        try:
            enabled = take("enabled", _is_bool)
            use_focus_targeting = take("use_focus_targeting", _is_bool)
            enable_offmonitor_fallback = take(
                "enable_offmonitor_fallback", _is_bool
            )
            min_confidence = take("min_confidence", _is_unit_float)
            clear_winner_margin = take("clear_winner_margin", _is_unit_float)
            tiebreaker_min_separation = take(
                "tiebreaker_min_separation_logical_px", _is_nonneg_int
            )
            tiebreaker_influence = take(
                "tiebreaker_influence_logical_px", _is_nonneg_int
            )
            notice_max_names = take("notice_max_names", _is_int_at_least(1))
            enable_screen_reader_flag = take("enable_screen_reader_flag", _is_bool)
            snapshot_ttl_seconds = take("snapshot_ttl_seconds", _is_int_at_least(1))
            response_timeout_ms = take("response_timeout_ms", _is_int_at_least(100))
            # walk_deadline_ms bounds the Input-side UIA click walk so it gives
            # up before the Logic-side click awaiter (wh-9f3t.54.2). The
            # cross-key invariant is walk_deadline_ms STRICTLY < response_timeout_ms
            # (FINDING 3): the two timers have DIFFERENT zero points -- the Logic
            # awaiter measures from IPC SEND, while the walk deadline (even when
            # anchored at command-dequeue) starts AFTER the SharedMemory
            # round-trip. Equality would leave zero slack for that pre-walk
            # latency, so the walk could finish AFTER the awaiter already timed
            # out. Requiring strict-less-than (and clamping the default below
            # response_timeout_ms by a margin) preserves a slack budget.
            # Validating AFTER response_timeout_ms lets the bound track a raised
            # awaiter timeout. Two cases:
            #   * EXPLICIT key: validated as an int in [100, response_timeout_ms)
            #     -- closed floor, OPEN (strict) upper, with the SAME
            #     _WALK_DEADLINE_MARGIN_MS slack the missing-key clamp applies
            #     (wh-9f3t.74.1). strict-less-than alone is necessary but not
            #     sufficient: an explicit walk_deadline_ms = response_timeout_ms
            #     - 1 passes equality-forbidding validation yet leaves ~0 slack,
            #     so the deadline expires AFTER the awaiter (the SharedMemory
            #     round-trip alone exceeds 1ms) and the bound is a no-op. A
            #     present value within the margin band (>= response_timeout_ms -
            #     margin) is a user error on an explicit value -> disable
            #     (never-raise), consistent with the disable-on-bad-value
            #     contract; the operator raises response_timeout_ms or lowers
            #     walk_deadline_ms to re-enable. The upper bound is floored at
            #     the 100 minimum so a tiny response_timeout_ms degrades to
            #     disable rather than an inverted range.
            #   * MISSING key: take() would return the raw 2500 default WITHOUT
            #     validating it, so a tightened response_timeout_ms would leave
            #     the default exceeding it and the bound INEFFECTIVE. Clamp the
            #     missing-key default DOWN to strictly below response_timeout_ms
            #     (leaving _WALK_DEADLINE_MARGIN_MS of slack, floored at 100) and
            #     keep clicking ENABLED -- a lowered awaiter timeout is a sensible
            #     config, not a fault. The clamp only ever lowers the default.
            if "walk_deadline_ms" in raw:
                walk_deadline_ms = take(
                    "walk_deadline_ms",
                    _is_int_below(
                        100,
                        max(100, response_timeout_ms - _WALK_DEADLINE_MARGIN_MS),
                    ),
                )
            else:
                walk_deadline_ms = _clamp_default_walk_deadline(
                    defaults["walk_deadline_ms"], response_timeout_ms
                )
            min_substring_query_length = take(
                "min_substring_query_length", _is_int_at_least(1)
            )
            min_substring_overlap_ratio = take(
                "min_substring_overlap_ratio", _is_unit_float
            )
            enable_coord_click = take(
                "enable_coordinate_click_on_com_error", _is_bool
            )
            browser_processes = take("browser_processes", _is_exe_list)
            browser_processes_extend = take(
                "browser_processes_extend", _is_exe_list
            )
        except _BadKey as bad:
            logger.error(
                "Invalid [click] config key %r (raw value %r); disabling voice "
                "clicking. Check config.toml [click] and correct this key.",
                bad.key,
                bad.value,
            )
            return _disabled(invalid_key=bad.key)

        # Overlay keys validate on a SEPARATE track (wh-n29v.29). A bad overlay
        # value does NOT raise _BadKey and does NOT disable by-name click; it is
        # collected into overlay_invalid_key and only the overlay is disabled.
        overlay = _validate_overlay(raw, defaults)

        return cls(
            enabled=enabled,
            use_focus_targeting=use_focus_targeting,
            enable_offmonitor_fallback=enable_offmonitor_fallback,
            min_confidence=min_confidence,
            clear_winner_margin=clear_winner_margin,
            tiebreaker_min_separation_logical_px=tiebreaker_min_separation,
            tiebreaker_influence_logical_px=tiebreaker_influence,
            notice_max_names=notice_max_names,
            enable_screen_reader_flag=enable_screen_reader_flag,
            snapshot_ttl_seconds=snapshot_ttl_seconds,
            response_timeout_ms=response_timeout_ms,
            walk_deadline_ms=walk_deadline_ms,
            min_substring_query_length=min_substring_query_length,
            min_substring_overlap_ratio=min_substring_overlap_ratio,
            enable_coordinate_click_on_com_error=enable_coord_click,
            browser_processes=browser_processes,
            browser_processes_extend=browser_processes_extend,
            invalid_key=None,
            overlay_enabled=overlay.overlay_enabled,
            overlay_badge_font_pt=overlay.overlay_badge_font_pt,
            overlay_badge_corner=overlay.overlay_badge_corner,
            overlay_badge_trailing_space=overlay.overlay_badge_trailing_space,
            overlay_badge_shadow=overlay.overlay_badge_shadow,
            overlay_auto_open_on_ambiguous=overlay.overlay_auto_open_on_ambiguous,
            overlay_focus_debounce_ms=overlay.overlay_focus_debounce_ms,
            overlay_bounds_tolerance_physical_px=(
                overlay.overlay_bounds_tolerance_physical_px
            ),
            snapshot_store_capacity=overlay.snapshot_store_capacity,
            overlay_browser_refresh_seconds=(
                overlay.overlay_browser_refresh_seconds
            ),
            overlay_invalid_key=overlay.overlay_invalid_key,
        )


class _BadKey(Exception):
    """Internal: a present key failed validation. Carries the key + raw value."""

    def __init__(self, key: str, value: Any) -> None:
        super().__init__(key)
        self.key = key
        self.value = value


# -- per-type validators -----------------------------------------------------
# Each returns (ok, coerced_value). bool is excluded from int/float checks
# because bool is a subclass of int in Python (the bool-is-int trap).


def _is_bool(value: Any) -> tuple[bool, Any]:
    return (isinstance(value, bool), value)


def _is_real_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_unit_float(value: Any) -> tuple[bool, Any]:
    """Float in [0.0, 1.0]. Accepts a real int (promoted); rejects bool."""
    if isinstance(value, bool):
        return (False, value)
    if isinstance(value, int):
        value = float(value)
    if not isinstance(value, float):
        return (False, value)
    if value != value:  # NaN never compares in-range; reject it.
        return (False, value)
    return (0.0 <= value <= 1.0, value)


def _is_nonneg_int(value: Any) -> tuple[bool, Any]:
    if not _is_real_int(value):
        return (False, value)
    return (value >= 0, value)


def _is_int_at_least(minimum: int) -> Any:
    def check(value: Any) -> tuple[bool, Any]:
        if not _is_real_int(value):
            return (False, value)
        return (value >= minimum, value)

    return check


def _is_int_below(minimum: int, exclusive_maximum: int) -> Any:
    """A real int in the half-open range [minimum, exclusive_maximum); rejects bool.

    Floor inclusive, ceiling EXCLUSIVE: ``minimum <= value < exclusive_maximum``.
    ``exclusive_maximum`` may be a value derived at validation time from another
    already-coerced key (e.g. walk_deadline_ms's upper bound is the effective
    response_timeout_ms), so the bound tracks that key. The strict upper bound
    forbids equality so walk_deadline_ms always leaves slack below the awaiter
    timeout (wh-9f3t.54.2 FINDING 3).
    """

    def check(value: Any) -> tuple[bool, Any]:
        if not _is_real_int(value):
            return (False, value)
        return (minimum <= value < exclusive_maximum, value)

    return check


def _is_int_in_range(minimum: int, maximum: int) -> Any:
    """A real int in the CLOSED range ``[minimum, maximum]``; rejects bool.

    Both ends inclusive. Used by the Phase 1.5 overlay keys, whose design-v4
    ranges are closed (e.g. overlay_badge_font_pt in [6, 96],
    overlay_focus_debounce_ms in [0, 5000] where 0 is the valid no-debounce
    value). bool is excluded via _is_real_int (the bool-is-int trap).
    """

    def check(value: Any) -> tuple[bool, Any]:
        if not _is_real_int(value):
            return (False, value)
        return (minimum <= value <= maximum, value)

    return check


# The slack (ms) the missing-key walk_deadline_ms clamp leaves below
# response_timeout_ms to cover the pre-walk latency (SharedMemory round-trip,
# foreground capture, ElementFromHandle) that the Logic awaiter's
# IPC-send-anchored timer already counts but the walk deadline does not start
# until command-dequeue (wh-9f3t.54.2 FINDING 3).
_WALK_DEADLINE_MARGIN_MS = 250

# The lowest value walk_deadline_ms is allowed to take (matches the floor of
# both response_timeout_ms and the explicit walk_deadline_ms validator).
_WALK_DEADLINE_FLOOR_MS = 100


def _clamp_default_walk_deadline(default: int, response_timeout_ms: int) -> int:
    """Clamp the missing-key walk_deadline_ms default strictly below the awaiter.

    Returns ``min(default, response_timeout_ms - _WALK_DEADLINE_MARGIN_MS)``,
    floored at ``_WALK_DEADLINE_FLOOR_MS`` so an aggressively tight
    response_timeout_ms still yields a usable (and strictly-less-than) deadline.
    Because response_timeout_ms >= 100 (its own validator floor) and the floor
    is 100, the result is always <= response_timeout_ms; when the margin would
    push it below the floor we clamp UP to the floor, which keeps it strictly
    below response_timeout_ms whenever response_timeout_ms > 100 and equal only
    in the degenerate response_timeout_ms == 100 case (the tightest legal
    awaiter, where 100 == 100 is the best achievable bound). The clamp only ever
    lowers the 2500 default, never raises it.
    """
    ceiling = response_timeout_ms - _WALK_DEADLINE_MARGIN_MS
    return max(_WALK_DEADLINE_FLOOR_MS, min(default, ceiling))


_BADGE_CORNERS: frozenset[str] = frozenset(
    {"top_left", "top_right", "bottom_left", "bottom_right"}
)


def _is_badge_corner(value: Any) -> tuple[bool, Any]:
    """A string naming one of the four badge corners. Anything else fails.

    The overlay number is anchored to this corner of the control it labels
    (wh-overlay-badge-occludes-label). A non-str or an unknown corner name is a
    validation failure that disables only the overlay, keeping by-name click on.
    """
    return (isinstance(value, str) and value in _BADGE_CORNERS, value)


def _is_exe_list(value: Any) -> tuple[bool, Any]:
    """A list of strings, each ending in ``.exe`` (case-insensitive) -> tuple.

    A tuple input is accepted too (defensive); any non-list/tuple, any non-str
    entry, or any entry not ending in ``.exe`` (case-insensitively) fails. The
    original casing of each accepted entry is preserved in the output.
    """
    if not isinstance(value, (list, tuple)):
        return (False, value)
    out: list[str] = []
    for entry in value:
        # Windows executable names are case-insensitive, so a case variant such
        # as "Chrome.EXE" is legitimate user input on the documented
        # browser_processes_extend extension surface; reject only a non-str
        # entry or a name that does not end in ".exe" case-insensitively
        # (wh-9f3t.30.2).
        if not isinstance(entry, str) or not entry.lower().endswith(".exe"):
            return (False, value)
        out.append(entry)
    return (True, tuple(out))


# -- defaults + sentinel ------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
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
    # walk_deadline_ms default is strictly below response_timeout_ms's default
    # (2500 < 3000) so the Input walk gives up before the Logic awaiter.
    "walk_deadline_ms": 2500,
    "min_substring_query_length": 4,
    "min_substring_overlap_ratio": 0.6,
    "enable_coordinate_click_on_com_error": False,
    "browser_processes": _DEFAULT_BROWSER_PROCESSES,
    "browser_processes_extend": (),
    # -- Phase 1.5 overlay defaults (wh-n29v.29, design-v4 Configuration) ----
    "overlay_enabled": True,
    "overlay_badge_font_pt": 16,
    "overlay_badge_corner": "top_right",
    "overlay_badge_trailing_space": True,
    "overlay_badge_shadow": True,
    "overlay_auto_open_on_ambiguous": True,
    "overlay_focus_debounce_ms": 250,
    "overlay_bounds_tolerance_physical_px": 8,
    "snapshot_store_capacity": 4,
    "overlay_browser_refresh_seconds": 10,
}


# Canonical overlay key -> its deprecated alias. The bounds-tolerance key was
# renamed *_logical_px -> *_physical_px (wh-bounds-tol-rename-physical: the
# compared units are physical UIA pixels), and an existing user config.toml
# may still set the old name. _validate_overlay reads the alias when the
# canonical key is absent, logs a deprecation warning naming both keys, and
# records the ALIAS name in overlay_invalid_key on a bad value (the operator
# log must name the key actually in the user's file). When both keys are
# present the canonical one wins and the ignored alias is named in a warning.
_DEPRECATED_OVERLAY_ALIASES: dict[str, str] = {
    "overlay_bounds_tolerance_physical_px": "overlay_bounds_tolerance_logical_px",
}


# Overlay key -> validator. overlay_enabled gates the effective flag but its own
# bad value (a non-bool) is still a validation failure that lands in
# overlay_invalid_key. The int ranges are CLOSED per design-v4; note 0 is a
# VALID overlay_focus_debounce_ms (no-debounce), so the floor is 0, not 1.
_OVERLAY_VALIDATORS: dict[str, Any] = {
    "overlay_enabled": _is_bool,
    "overlay_badge_font_pt": _is_int_in_range(6, 96),
    "overlay_badge_corner": _is_badge_corner,
    "overlay_badge_trailing_space": _is_bool,
    "overlay_badge_shadow": _is_bool,
    "overlay_auto_open_on_ambiguous": _is_bool,
    "overlay_focus_debounce_ms": _is_int_in_range(0, 5000),
    "overlay_bounds_tolerance_physical_px": _is_int_in_range(0, 200),
    "snapshot_store_capacity": _is_int_in_range(1, 64),
    # wh-n29v.121: 0 is a VALID value (proactive refresh off), so the floor
    # is 0, not 1.
    "overlay_browser_refresh_seconds": _is_int_in_range(0, 300),
}


@dataclass(frozen=True)
class _OverlayResult:
    """Internal carrier for the overlay-track validation outcome.

    Carries the coerced overlay field values; the effective gate is NOT carried
    here, because ClickConfig.overlay_enabled_effective derives it from
    overlay_enabled and overlay_invalid_key (wh-n29v.32.1, wh-n29v.32.2).
    """

    overlay_enabled: bool
    overlay_badge_font_pt: int
    overlay_badge_corner: str
    overlay_badge_trailing_space: bool
    overlay_badge_shadow: bool
    overlay_auto_open_on_ambiguous: bool
    overlay_focus_debounce_ms: int
    overlay_bounds_tolerance_physical_px: int
    snapshot_store_capacity: int
    overlay_browser_refresh_seconds: int
    overlay_invalid_key: tuple[str, ...]


def _validate_overlay(raw: dict[str, Any], defaults: dict[str, Any]) -> _OverlayResult:
    """Validate the ten overlay keys on a separate track from Phase 1.

    A MISSING overlay key takes its default and never lands in
    overlay_invalid_key. A PRESENT-but-bad overlay key keeps its default value
    for the field AND appends its name to overlay_invalid_key (collecting ALL
    bad keys, not just the first). The effective overlay gate is NOT computed
    here: ClickConfig.overlay_enabled_effective derives it from overlay_enabled
    and overlay_invalid_key, so a valid overlay_enabled=false is an opt-out
    (effective False, empty invalid-key tuple) and a bad value disables the
    overlay without touching Phase 1's enabled flag.
    """
    coerced: dict[str, Any] = {}
    overlay_invalid_key: list[str] = []
    for key, validate in _OVERLAY_VALIDATORS.items():
        # Alias pre-pass (wh-bounds-tol-rename-physical): source_key is the
        # key whose raw value is validated -- the canonical name when present,
        # else its deprecated alias. All logging below uses source_key so the
        # operator log names the key actually written in the user's file.
        alias = _DEPRECATED_OVERLAY_ALIASES.get(key)
        source_key = key
        if key not in raw:
            if alias is not None and alias in raw:
                source_key = alias
                logger.warning(
                    "[click] config key %r is deprecated; rename it to %r "
                    "(same value, same meaning -- the compared units were "
                    "always physical pixels). The old key still works this "
                    "session.",
                    alias,
                    key,
                )
            else:
                coerced[key] = defaults[key]
                continue
        elif alias is not None and alias in raw:
            logger.warning(
                "[click] config sets both %r and its deprecated alias %r; "
                "using %r and ignoring the alias.",
                key,
                alias,
                key,
            )
        ok, value = validate(raw[source_key])
        if ok:
            coerced[key] = value
        else:
            # Keep the default for the field; record the bad key. The error is
            # logged so the (deferred) startup-notice surface and the operator
            # log both name the offending overlay key.
            logger.error(
                "Invalid [click] overlay config key %r (raw value %r); disabling "
                "the numbered overlay only. By-name voice clicking stays active. "
                "Check config.toml [click] and correct this key.",
                source_key,
                raw[source_key],
            )
            coerced[key] = defaults[key]
            overlay_invalid_key.append(source_key)

    overlay_enabled = coerced["overlay_enabled"]
    return _OverlayResult(
        overlay_enabled=overlay_enabled,
        overlay_badge_font_pt=coerced["overlay_badge_font_pt"],
        overlay_badge_corner=coerced["overlay_badge_corner"],
        overlay_badge_trailing_space=coerced["overlay_badge_trailing_space"],
        overlay_badge_shadow=coerced["overlay_badge_shadow"],
        overlay_auto_open_on_ambiguous=coerced["overlay_auto_open_on_ambiguous"],
        overlay_focus_debounce_ms=coerced["overlay_focus_debounce_ms"],
        overlay_bounds_tolerance_physical_px=coerced[
            "overlay_bounds_tolerance_physical_px"
        ],
        snapshot_store_capacity=coerced["snapshot_store_capacity"],
        overlay_browser_refresh_seconds=coerced["overlay_browser_refresh_seconds"],
        # Convert the mutable accumulator to an immutable tuple for the frozen
        # ClickConfig field (keeps every ClickConfig hashable; see the field
        # comment on overlay_invalid_key).
        overlay_invalid_key=tuple(overlay_invalid_key),
    )


def _disabled(*, invalid_key: Optional[str]) -> ClickConfig:
    """Build a disabled ClickConfig at safe defaults, recording invalid_key."""
    return ClickConfig(
        enabled=False,
        use_focus_targeting=_DEFAULTS["use_focus_targeting"],
        enable_offmonitor_fallback=_DEFAULTS["enable_offmonitor_fallback"],
        min_confidence=_DEFAULTS["min_confidence"],
        clear_winner_margin=_DEFAULTS["clear_winner_margin"],
        tiebreaker_min_separation_logical_px=(
            _DEFAULTS["tiebreaker_min_separation_logical_px"]
        ),
        tiebreaker_influence_logical_px=_DEFAULTS["tiebreaker_influence_logical_px"],
        notice_max_names=_DEFAULTS["notice_max_names"],
        enable_screen_reader_flag=_DEFAULTS["enable_screen_reader_flag"],
        snapshot_ttl_seconds=_DEFAULTS["snapshot_ttl_seconds"],
        response_timeout_ms=_DEFAULTS["response_timeout_ms"],
        walk_deadline_ms=_DEFAULTS["walk_deadline_ms"],
        min_substring_query_length=_DEFAULTS["min_substring_query_length"],
        min_substring_overlap_ratio=_DEFAULTS["min_substring_overlap_ratio"],
        enable_coordinate_click_on_com_error=(
            _DEFAULTS["enable_coordinate_click_on_com_error"]
        ),
        browser_processes=_DEFAULTS["browser_processes"],
        browser_processes_extend=_DEFAULTS["browser_processes_extend"],
        invalid_key=invalid_key,
        # Overlay fields on the disabled path: the overlay is effectively off
        # whenever the feature is disabled. overlay_enabled is set to False (NOT
        # its True default) so the DERIVED overlay_enabled_effective property
        # returns False in EVERY disabled case -- DISABLED_CLICK_CONFIG, a
        # non-table raw, and a Phase 1 validation failure (wh-n29v.31.1).
        # overlay_invalid_key is an empty tuple.
        overlay_enabled=False,
        overlay_badge_font_pt=_DEFAULTS["overlay_badge_font_pt"],
        overlay_badge_corner=_DEFAULTS["overlay_badge_corner"],
        overlay_badge_trailing_space=_DEFAULTS["overlay_badge_trailing_space"],
        overlay_badge_shadow=_DEFAULTS["overlay_badge_shadow"],
        overlay_auto_open_on_ambiguous=_DEFAULTS["overlay_auto_open_on_ambiguous"],
        overlay_focus_debounce_ms=_DEFAULTS["overlay_focus_debounce_ms"],
        overlay_bounds_tolerance_physical_px=(
            _DEFAULTS["overlay_bounds_tolerance_physical_px"]
        ),
        snapshot_store_capacity=_DEFAULTS["snapshot_store_capacity"],
        overlay_browser_refresh_seconds=(
            _DEFAULTS["overlay_browser_refresh_seconds"]
        ),
        overlay_invalid_key=(),
    )


# Module-level sentinel: a disabled config at safe defaults, no validation
# failure recorded. Use this where a caller needs a ClickConfig but has no raw
# block to validate (e.g. the feature was never configured).
DISABLED_CLICK_CONFIG: ClickConfig = _disabled(invalid_key=None)


__all__ = [
    "ClickConfig",
    "DISABLED_CLICK_CONFIG",
]
