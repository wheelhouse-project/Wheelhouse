"""WorkingIndicatorConfig -- fail-soft validator for the dictation-retraction
working indicator (wh-dictation-retraction-indicator.4).

The working indicator paints a small busy/working glyph at the mouse pointer
while dictated text is still provisional (the live words could be retracted by
the STT final), so a retraction is less surprising. Its only setting today is
an on/off toggle in the ``[dictation]`` block of ``config.toml``::

    [dictation]
    working_indicator_enabled = true   # default; set false to turn it off

Why a feature-init validator and not ``ConfigService``
======================================================
``ConfigService`` is a raw ``tomllib.load`` wrapper that returns an unchecked
dict with no per-feature degrade path. This validator turns that raw block into
a typed value without ever raising, mirroring ``ui/click_config.py``: the
indicator is a non-critical cosmetic affordance, so a malformed value must never
crash the GUI process (which also owns the tray, the floating button, and the
dictation editor).

Contract (``from_raw`` NEVER raises)
====================================
* Missing key (empty / absent ``[dictation]`` block) -> default ON. An absent
  key is config-author omission, not a malformed value.
* A valid ``bool`` -> that value. ``working_indicator_enabled = false`` is a
  legitimate operator opt-out, not a fault.
* A present-but-bad value (non-``bool``) -> the default, plus a ``logger.warning``
  naming the key. This includes the Python bool-is-int trap: a raw ``1`` / ``0``
  is an ``int``, not a ``bool``, so it is rejected rather than silently read as
  ``true`` / ``false``.
* A non-mapping raw block (the whole ``[dictation]`` value is malformed) ->
  the default, defensively.

Default ON because the indicator exists precisely to reduce the surprise of a
retraction the user reported; it is trivially disabled with one config line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Config key inside the [dictation] block.
_KEY_ENABLED = "working_indicator_enabled"
# Default: the indicator is ON unless the operator opts out.
_DEFAULT_ENABLED = True


@dataclass(frozen=True)
class WorkingIndicatorConfig:
    """Validated settings for the dictation working indicator."""

    enabled: bool = _DEFAULT_ENABLED

    @classmethod
    def from_raw(cls, raw: object) -> "WorkingIndicatorConfig":
        """Build a validated config from the raw ``[dictation]`` block.

        Never raises (see module docstring). ``raw`` is whatever
        ``ConfigService`` returned for the ``[dictation]`` key -- normally a
        dict, but defended against any type.
        """
        if not isinstance(raw, dict):
            # A malformed [dictation] block (not a table): use the default.
            return cls()

        if _KEY_ENABLED not in raw:
            return cls()

        value = raw[_KEY_ENABLED]
        # bool-is-int trap: reject a non-bool (e.g. int 1/0, str "true") so it
        # is not silently coerced; degrade to the default and surface it.
        if not isinstance(value, bool):
            logger.warning(
                "config [dictation] %s=%r is not a boolean; using default %r",
                _KEY_ENABLED,
                value,
                _DEFAULT_ENABLED,
            )
            return cls()

        return cls(enabled=value)
