"""ActionsConfig feature-init validator for the [actions] block (wh-actions-config).

The pattern-actions expansion (spec:
docs/superpowers/specs/2026-08-09-pattern-actions-expansion-design.md,
section 6) reads its two settings from the ``[actions]`` block of
``config.toml``. This module turns the raw, unchecked value that
``ConfigService`` hands back into a typed, range-checked
:class:`ActionsConfig`, following the feature-init-validator precedent of
``ui/click_config.py``: ``ConfigService`` is a raw ``tomllib.load`` wrapper
with no per-feature degrade path, so validation lives here, fail-soft.

The never-raises contract:
==========================
``ActionsConfig.from_raw`` NEVER raises. Unlike ``ClickConfig`` there is no
feature to disable -- both keys are operating parameters for actions that
must keep working -- so every key degrades independently:

* A MISSING key (or a missing/empty ``[actions]`` section) falls back to its
  default silently. Absence is config-author omission, not a fault.
* A PRESENT key of the WRONG TYPE logs ``logger.error`` naming the key and
  falls back to that key's default. The other key is unaffected.
* A PRESENT key of the right type but OUT OF RANGE is CLAMPED into the legal
  range with a ``logger.warning`` -- this is the spec's "adjustable in
  config.toml but clamped below what the message limit can carry".

bool-is-int trap (Python: ``bool`` is a subclass of ``int``): the int-typed
cap rejects a ``bool``; the float-typed timeout accepts a real ``int``
(promoted to float) but rejects ``bool`` and NaN.

The output-cap ceiling:
=======================
Captured text ultimately crosses to the Input process inside the 64 KiB
Logic-to-Input SharedMemory message (``launcher.py`` ``SHARED_MEM_SIZE``).
The cap counts CHARACTERS; a character is up to 4 bytes in UTF-8, and the
pickled command envelope around the text needs room of its own. The ceiling
therefore reserves 4 KiB for the envelope and divides the rest by the
4-bytes-per-char worst case: (64 KiB - 4 KiB) / 4 = 15360 characters.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Spec section 6 defaults.
DEFAULT_OUTPUT_CAP_CHARS = 10000
DEFAULT_RUN_CAPTURE_TIMEOUT_S = 10.0

# Largest cap that keeps a worst-case 4-bytes-per-char UTF-8 payload inside
# the 64 KiB Logic-to-Input message with 4 KiB of envelope headroom (see the
# module docstring). 15360.
OUTPUT_CAP_CEILING_CHARS = (64 * 1024 - 4 * 1024) // 4

# The waiting-action clamp range from spec sections 2.3 and 3.1: every
# waiting action honors the 60-second ceiling, and the shortest meaningful
# subprocess timeout is 0.1 s. The config default clamps into the same range
# the leading-number timeout parameter does.
RUN_CAPTURE_TIMEOUT_FLOOR_S = 0.1
RUN_CAPTURE_TIMEOUT_CEILING_S = 60.0


@dataclass(frozen=True)
class ActionsConfig:
    """Validated, immutable [actions] configuration.

    ``output_cap_chars`` is the shared cap on stored action output
    (run_capture, ask_ai); exceeding it at capture time is a step FAILURE,
    not a truncation (spec 2.4). ``run_capture_timeout_default_s`` is the
    run_capture wait when a pattern gives no leading-number timeout.
    """

    output_cap_chars: int = DEFAULT_OUTPUT_CAP_CHARS
    run_capture_timeout_default_s: float = DEFAULT_RUN_CAPTURE_TIMEOUT_S

    @classmethod
    def from_raw(cls, raw: Any) -> "ActionsConfig":
        """Validate a raw ``[actions]`` value; never raises.

        ``raw`` is typed ``Any`` deliberately: ``ConfigService`` returns
        ``Any`` and a malformed ``[actions]`` value can be a non-table (a
        scalar or list). Callers pass ``config_service.get("actions", {})``.
        """
        if not isinstance(raw, dict):
            if raw is not None:
                logger.error(
                    "[actions] config block is a %s, not a table; using the "
                    "defaults for every [actions] key.",
                    type(raw).__name__,
                )
            return cls()
        try:
            return cls(
                output_cap_chars=_validate_output_cap(raw),
                run_capture_timeout_default_s=_validate_timeout_default(raw),
            )
        except Exception:  # noqa: BLE001 -- the contract is to NEVER raise
            logger.error(
                "Unexpected error validating the [actions] config block; "
                "using the defaults for every [actions] key.",
                exc_info=True,
            )
            return cls()


def _is_real_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_output_cap(raw: dict[str, Any]) -> int:
    key = "output_cap_chars"
    if key not in raw:
        return DEFAULT_OUTPUT_CAP_CHARS
    value = raw[key]
    if not _is_real_int(value):
        logger.error(
            "Invalid [actions] config key %r (raw value %r); expected a whole "
            "number, using the default %d.",
            key,
            value,
            DEFAULT_OUTPUT_CAP_CHARS,
        )
        return DEFAULT_OUTPUT_CAP_CHARS
    clamped = max(1, min(value, OUTPUT_CAP_CEILING_CHARS))
    if clamped != value:
        logger.warning(
            "[actions] config key %r value %d is outside 1..%d; clamped to %d "
            "(the ceiling keeps captured text inside the %d KiB message to "
            "the input process).",
            key,
            value,
            OUTPUT_CAP_CEILING_CHARS,
            clamped,
            64,
        )
    return clamped


def _validate_timeout_default(raw: dict[str, Any]) -> float:
    key = "run_capture_timeout_default_s"
    if key not in raw:
        return DEFAULT_RUN_CAPTURE_TIMEOUT_S
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.error(
            "Invalid [actions] config key %r (raw value %r); expected a "
            "number of seconds, using the default %g.",
            key,
            value,
            DEFAULT_RUN_CAPTURE_TIMEOUT_S,
        )
        return DEFAULT_RUN_CAPTURE_TIMEOUT_S
    if isinstance(value, float) and math.isnan(value):
        logger.error(
            "Invalid [actions] config key %r (raw value %r); expected a "
            "number of seconds, using the default %g.",
            key,
            value,
            DEFAULT_RUN_CAPTURE_TIMEOUT_S,
        )
        return DEFAULT_RUN_CAPTURE_TIMEOUT_S
    # Clamp BEFORE converting to float: tomllib parses TOML integers at
    # arbitrary precision, and float() (like math.isnan) raises OverflowError
    # on an int too large for a float. int/float comparisons are exact at any
    # magnitude, so the min/max never overflows and the clamped result is
    # always small enough to convert.
    clamped = float(
        max(RUN_CAPTURE_TIMEOUT_FLOOR_S, min(value, RUN_CAPTURE_TIMEOUT_CEILING_S))
    )
    if clamped != value:
        logger.warning(
            "[actions] config key %r value %s is outside %g..%g seconds; "
            "clamped to %g (waiting actions honor the 60-second ceiling).",
            key,
            value,
            RUN_CAPTURE_TIMEOUT_FLOOR_S,
            RUN_CAPTURE_TIMEOUT_CEILING_S,
            clamped,
        )
    return clamped


__all__ = [
    "ActionsConfig",
    "DEFAULT_OUTPUT_CAP_CHARS",
    "DEFAULT_RUN_CAPTURE_TIMEOUT_S",
    "OUTPUT_CAP_CEILING_CHARS",
    "RUN_CAPTURE_TIMEOUT_FLOOR_S",
    "RUN_CAPTURE_TIMEOUT_CEILING_S",
]
