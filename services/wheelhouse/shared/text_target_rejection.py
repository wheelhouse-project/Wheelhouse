"""text_target_rejected IPC event schema (wh-hqipv).

Defines the unsolicited Input -> Logic event emitted when an insertion
strategy rejects a focused control as not a valid text target. The
schema is the contract for Phase 2 of wh-9weum (text-target gate
relaxation and override toast). The payload carries the focused
control's identity so the GUI can render branched advisory wording and
so a Phase 4 retry-click can correlate back to the original rejection.

Transport: the Input Process puts a dict produced by
``TextTargetRejectedEvent.to_dict()`` onto the response queue. The
Logic Process inspects ``msg.get("type")`` in
``LogicController._handle_input_event`` and forwards the dict to the
GUI on the state queue. The receiver should call
``TextTargetRejectedEvent.from_dict()`` and catch
``TextTargetRejectedSchemaError`` for graceful degradation on a
malformed payload (wh-uf54).

Field meanings:
  * ``process_name`` -- short name of the process owning the focused
    control (e.g. ``"zed.exe"``).
  * ``class_name`` -- the UIA ClassName of the control. May be empty
    for the browser-empty-ClassName trap (wh-zndq).
  * ``control_type`` -- the UIA ControlType (e.g. ``"Pane"``,
    ``"Button"``, ``"Edit"``).
  * ``reason`` -- short machine-readable tag explaining why the
    control was rejected. The GUI uses this to pick branched wording.
    Values are not enumerated here so reviewers can adjust the set
    without touching the schema. The Phase 2 child beads (wh-7318z,
    wh-lzsbd) define the concrete tags.
  * ``supported_patterns`` -- tuple of UIA pattern names the control
    advertised. Empty tuple is valid and is itself diagnostic ("the
    control supports nothing"). Provided for both diagnostic toast
    text and for the verified-retry counter in Phase 4.
  * ``app_friendly_name`` -- the human-readable application name
    resolved via ``GetFileVersionInfo`` (wh-b0sch). Falls back to
    ``process_name`` when version info is unavailable.
  * ``correlation_token`` -- uuid4 string assigned at rejection time.
    Threads the round trip from rejection event through the optional
    Phase 4 ``retry_dictation_by_token`` request (wh-wt82) so the
    Logic process can match the click to the rejection that produced
    the toast.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.correlation_token import (
    validate_correlation_token,
)


MSG_TYPE = "text_target_rejected"


# ---------------------------------------------------------------------------
# Reason tag constants (wh-z7qx1).
# ---------------------------------------------------------------------------
#
# The text-target predicate (``services/wheelhouse/ui/text_target.py``)
# emits these strings on the ``reason`` field of a rejection event.
# Centralising them here lets the GUI's Try-it-anyway visibility
# branching (Phase 4) and any future callers reference the names
# without re-spelling the literals.
#
# These constants are deliberately ``str`` rather than an ``Enum`` so
# the IPC schema stays string-typed (the wire format is JSON-friendly
# and the schema validator already checks that ``reason`` is a string).

REASON_STALE_COM = "stale_com"
"""COM call into the focused control raised; the snapshot is unusable."""

REASON_NOT_FOCUSABLE = "not_focusable"
"""Control reports it cannot accept keyboard focus."""

REASON_DENYLIST_CLASS_NAME = "denylist_class_name"
"""ClassName matches the denylist (e.g. ``MenuFlyoutSubItem``)."""

REASON_DENYLIST_CONTROL_TYPE = "denylist_control_type"
"""ControlType matches the denylist (button, menu item, list item)."""

REASON_DEFAULT_REJECT = "default_reject"
"""Generic reject. In a browser process with empty ClassName this is
the wh-zndq browser-empty-ClassName trap; elsewhere it is the empty-
ClassName non-browser case."""

REASON_DEFAULT_REJECT_PASTE_CAPABLE_CLASS = "default_reject_paste_capable_class"
"""Soft reject: ClassName has positive paste-capable signal but no
soft-allow tuple loaded yet. Phase 4's Try-it-anyway button is
visible only on this category."""


class TextTargetRejectedSchemaError(ValueError):
    """Raised by ``TextTargetRejectedEvent.from_dict`` on a malformed payload.

    The Logic and GUI processes should catch this and degrade
    gracefully (log + drop), per wh-uf54.
    """


@dataclass(frozen=True)
class TextTargetRejectedEvent:
    """Structured payload of a text_target_rejected IPC event."""

    process_name: str
    class_name: str
    control_type: str
    reason: str
    supported_patterns: tuple[str, ...]
    app_friendly_name: str
    correlation_token: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        The returned dict is suitable for placing on the input ->
        logic response queue. The ``"type"`` key carries
        ``MSG_TYPE`` so the existing dispatch in
        ``LogicController._handle_input_event`` can route it.
        """

        return {
            "type": MSG_TYPE,
            "process_name": self.process_name,
            "class_name": self.class_name,
            "control_type": self.control_type,
            "reason": self.reason,
            "supported_patterns": self.supported_patterns,
            "app_friendly_name": self.app_friendly_name,
            "correlation_token": self.correlation_token,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "TextTargetRejectedEvent":
        """Parse and validate a wire-format dict.

        Raises ``TextTargetRejectedSchemaError`` on any structural
        problem: not a mapping, missing ``"type"`` or a wrong
        ``"type"`` value, missing required field, wrong field type,
        or non-string member of ``supported_patterns``.
        ``supported_patterns`` is normalized to a tuple, so a
        sender that uses a list (e.g. through a JSON-bridged
        transport) is accepted.
        """

        if not isinstance(payload, Mapping):
            raise TextTargetRejectedSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "type" not in payload:
            raise TextTargetRejectedSchemaError(
                "payload missing required key 'type'"
            )
        if payload["type"] != MSG_TYPE:
            raise TextTargetRejectedSchemaError(
                f"payload type {payload['type']!r} does not match {MSG_TYPE!r}"
            )

        # Plain string fields. correlation_token is also a string but
        # additionally must match the uuid4 contract; it is validated
        # separately below.
        string_fields = (
            "process_name",
            "class_name",
            "control_type",
            "reason",
            "app_friendly_name",
        )
        for name in string_fields:
            if name not in payload:
                raise TextTargetRejectedSchemaError(
                    f"payload missing required field {name!r}"
                )
            value = payload[name]
            if not isinstance(value, str):
                raise TextTargetRejectedSchemaError(
                    f"field {name!r} must be a str, got {type(value).__name__}"
                )

        if "correlation_token" not in payload:
            raise TextTargetRejectedSchemaError(
                "payload missing required field 'correlation_token'"
            )
        token = validate_correlation_token(
            payload["correlation_token"],
            field_name="correlation_token",
            error_class=TextTargetRejectedSchemaError,
        )

        if "supported_patterns" not in payload:
            raise TextTargetRejectedSchemaError(
                "payload missing required field 'supported_patterns'"
            )
        raw_patterns = payload["supported_patterns"]
        # wh-9weum.1.1: a malformed iterable can raise TypeError or
        # RuntimeError during iteration, which would escape safe_parse
        # (catches ValueError only). Restrict the accepted shapes to
        # list / tuple of str so the iteration is safe; the documented
        # wire shapes (tuple from to_dict, list from a JSON-bridged
        # transport) both pass.
        if not isinstance(raw_patterns, (list, tuple)):
            raise TextTargetRejectedSchemaError(
                "field 'supported_patterns' must be a list or tuple of str, "
                f"got {type(raw_patterns).__name__}"
            )
        patterns: list[str] = []
        for member in raw_patterns:
            if not isinstance(member, str):
                raise TextTargetRejectedSchemaError(
                    "field 'supported_patterns' contains non-string member: "
                    f"{type(member).__name__}"
                )
            patterns.append(member)

        return cls(
            process_name=payload["process_name"],
            class_name=payload["class_name"],
            control_type=payload["control_type"],
            reason=payload["reason"],
            supported_patterns=tuple(patterns),
            app_friendly_name=payload["app_friendly_name"],
            correlation_token=token,
        )


def new_correlation_token() -> str:
    """Generate a fresh uuid4 correlation token as a string."""

    return str(uuid.uuid4())
