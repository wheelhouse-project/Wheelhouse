"""MouseActionResponse IPC schema (wh-input-mouse-primitives).

Defines the Input -> Logic reply shared by the three mouse-grid pointer
actions: ``click_point``, ``move_pointer``, and ``perform_drag``. The
authoritative feature spec is
``docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md`` under
"Input process -- the only place that touches the mouse".

One schema serves all three actions because all three answer the same
question: did the pointer operation happen, and if not, why. The envelope's
``action`` key (attached by the handler alongside ``request_id``) says which
operation the reply belongs to, so the payload does not repeat it.

Like ``ClickElementResponse`` and ``PinSnapshotResponse`` this is a
request-correlated Schema A response: the Input-process handler emits exactly
one response per ``request_id`` via the ``_HANDLES_OWN_RESPONSE`` machinery
and Logic's awaiter correlates it by ``request_id``. Because the correlation
rides the request_id envelope rather than a payload ``type`` key, this schema
carries no routing ``type`` field; ``status`` is the Schema A status field.

Transport: the Input Process puts a dict produced by
``MouseActionResponse.to_dict()`` onto the response queue. The Logic Process
calls ``MouseActionResponse.from_dict()`` and catches
``MouseActionResponseSchemaError`` for graceful degradation (log + drop), per
wh-uf54. ``from_dict`` never lets a raw ``KeyError`` / ``TypeError`` /
``AttributeError`` escape.

Field meanings:
  * ``status`` -- Schema A transport, ``"ok"`` or ``"error"``. Closed set.
  * ``outcome`` -- ``"ok"`` when the pointer operation was performed,
    ``"execution_failed"`` otherwise. Closed set. There is no ``not_found``
    or ``ambiguous`` outcome here: a grid operation acts on a point the user
    picked visually, so there is nothing to search for and nothing to
    disambiguate (the spec's "Verification difference" section).
  * ``reason`` -- machine-readable tag (``str``) set when
    ``outcome == "execution_failed"``, ``None`` otherwise. The set is OPEN
    (Logic must tolerate an unrecognized tag), matching the sibling
    ``ClickElementResponse.reason``. The tags the handlers emit today are:
    ``invalid_point``, ``invalid_button``, ``invalid_click_count``,
    ``invalid_duration``, ``disabled_by_config``, ``cursor_did_not_land``,
    ``sendinput_short``, ``sendinput_error``, ``release_failed``, and
    ``unexpected_error``. ``release_failed`` is the one tag that means a
    mouse button may still be held down; every other failure guarantees no
    button is left pressed.
  * ``trace_id`` -- the Logic-generated trace id so the Input-emitted log
    lines and the Logic awaiter share one correlation id, exactly as
    ``ClickElementResponse`` does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.schema_guard import reraise_as_schema_error


class MouseActionResponseSchemaError(ValueError):
    """Raised by ``MouseActionResponse.from_dict`` on a malformed payload.

    The Logic process should catch this and degrade gracefully (log + drop),
    per wh-uf54. ``from_dict`` never lets a raw ``KeyError`` / ``TypeError`` /
    ``AttributeError`` escape.
    """


_ALLOWED_STATUS = frozenset({"ok", "error"})
_ALLOWED_OUTCOME = frozenset({"ok", "execution_failed"})


@dataclass(frozen=True)
class MouseActionResponse:
    """Structured Input -> Logic reply for a mouse-grid pointer action."""

    status: str
    outcome: str
    reason: str | None
    trace_id: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict (JSON-friendly primitives)."""

        return {
            "status": self.status,
            "outcome": self.outcome,
            "reason": self.reason,
            "trace_id": self.trace_id,
        }

    @classmethod
    @reraise_as_schema_error(MouseActionResponseSchemaError)
    def from_dict(cls, payload: Any) -> "MouseActionResponse":
        """Parse and validate a wire-format dict.

        Raises ``MouseActionResponseSchemaError`` on any structural problem:
        not a mapping, a missing required field, a wrong field type, or a
        ``status`` / ``outcome`` value outside its closed set. Extra keys are
        ignored -- the handler attaches ``request_id`` and ``action`` to this
        same dict before enqueueing it.
        """

        if not isinstance(payload, Mapping):
            raise MouseActionResponseSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        status = _require_closed_str(payload, "status", _ALLOWED_STATUS)
        outcome = _require_closed_str(payload, "outcome", _ALLOWED_OUTCOME)
        reason = _require_optional_str(payload, "reason")
        trace_id = _require_str(payload, "trace_id")

        # The handler emits exactly two shapes (wh-mouse-grid.1.22): success
        # is status=ok + outcome=ok + reason=None; failure is status=error +
        # outcome=execution_failed + a nonempty reason. Validating the three
        # fields independently accepted impossible hybrids -- and because the
        # Logic awaiter decides success from `outcome` alone, a corrupted
        # status=error/outcome=ok reply was silently treated as a completed
        # click with its reason (possibly release_failed) discarded. The
        # reason TAG set stays open: any nonempty string is a valid tag.
        if status == "ok":
            if outcome != "ok" or reason is not None:
                raise MouseActionResponseSchemaError(
                    f"status 'ok' requires outcome 'ok' and reason None, "
                    f"got outcome {outcome!r} reason {reason!r}"
                )
        else:
            if outcome != "execution_failed" or not reason:
                raise MouseActionResponseSchemaError(
                    f"status 'error' requires outcome 'execution_failed' "
                    f"and a nonempty reason, got outcome {outcome!r} "
                    f"reason {reason!r}"
                )

        return cls(
            status=status,
            outcome=outcome,
            reason=reason,
            trace_id=trace_id,
        )


def _require_str(payload: Mapping[Any, Any], key: str) -> str:
    if key not in payload:
        raise MouseActionResponseSchemaError(
            f"payload missing required field {key!r}"
        )
    value = payload[key]
    if not isinstance(value, str):
        raise MouseActionResponseSchemaError(
            f"field {key!r} must be a str, got {type(value).__name__}"
        )
    return value


def _require_closed_str(
    payload: Mapping[Any, Any], key: str, allowed: frozenset[str]
) -> str:
    value = _require_str(payload, key)
    if value not in allowed:
        raise MouseActionResponseSchemaError(
            f"field {key!r} must be one of {sorted(allowed)}, got {value!r}"
        )
    return value


def _require_optional_str(payload: Mapping[Any, Any], key: str) -> str | None:
    if key not in payload:
        raise MouseActionResponseSchemaError(
            f"payload missing required field {key!r}"
        )
    value = payload[key]
    if value is None:
        return None
    # bool is not a str, so it is caught by the isinstance check below; the
    # explicit note is here because bool sneaks past int checks elsewhere.
    if not isinstance(value, str):
        raise MouseActionResponseSchemaError(
            f"field {key!r} must be a str or None, got {type(value).__name__}"
        )
    return value
