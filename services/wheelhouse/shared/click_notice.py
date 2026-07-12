"""ClickNoticeEvent IPC schema (wh-lstwt).

Defines the Logic -> GUI notice for a ``click_element`` non-ok outcome,
Phase 1 of the voice-element-clicking feature (epic wh-l4h.1). The
authoritative field spec lives in the v5 design doc:
``docs/plans/2026-05-21-voice-element-clicking-design-v5.md`` under
"Click notice IPC schema".

This is a NEW, dedicated path -- deliberately separate from the
``text_target_rejected`` rejection notice. Click outcomes have different
fields (matched names, snapshot reference) and -- critically -- NO
``correlation_token`` and NO Try-it-anyway retry-on-click semantics. The
click notice is advisory only; if a later Phase 1.5 adds a "click number
N" follow-up it uses ``snapshot_id`` directly, not a token. Reusing the
rejection schema would either drop the click payload as malformed or
render it with the wrong wording.

Transport: the Logic Process puts a dict produced by
``ClickNoticeEvent.to_dict()`` onto the state queue to the GUI Process,
which calls ``ClickNoticeEvent.from_dict()`` and catches
``ClickNoticeSchemaError`` for graceful degradation (log + drop) on a
malformed payload, per wh-uf54.

Field meanings:
  * ``outcome`` -- one of ``"not_found"``, ``"ambiguous"``, or
    ``"execution_failed"`` (closed set). ``"ok"`` is deliberately NOT a
    member: a successful click shows no notice, so ``"ok"`` never
    travels on this schema.
  * ``reason`` -- machine-readable tag (``str``) set when
    ``outcome == "execution_failed"`` (e.g. ``"disabled"``,
    ``"bounds_invalid"``, ``"foreground_changed"``,
    ``"foreground_verification_failed"``, ``"invoke_com_error"``,
    ``"invoke_then_sendinput_failed"``, ``"sendinput_short"``,
    ``"target_moved_offscreen"``, ``"timeout"``); ``None`` otherwise.
    The concrete tags are NOT enumerated in the schema so the executor
    can adjust the set without touching the contract -- the wording
    helper (``click_notice_toast_wording.py``) owns the tag-to-string
    mapping. This mirrors the sibling ``click_element.py``, which also
    leaves its ``reason`` value-domain open.
  * ``matched_name`` -- the single matched name used for
    ``execution_failed`` notice wording; ``None`` otherwise.
  * ``matched_names`` -- tuple of matched control names (for the
    ``ambiguous`` outcome). Empty tuple is valid. Normalized from a list
    on a JSON-bridged transport.
  * ``spoken_name`` -- the user's spoken target, used for the
    ``not_found`` wording.
  * ``app_friendly_name`` -- resolved via
    ``services/wheelhouse/utils/file_version_info.py``.
  * ``snapshot_id`` -- the walk snapshot id so the GUI can offer a
    Phase 1.5 "click number N" follow-up; ``None`` when no walk
    produced one.
  * ``trace_id`` -- the Logic-generated trace id (wh-l4h.1.9.5) so the
    Logic and GUI log lines share one correlation id. This schema only
    carries and round-trips the id; it does NOT generate it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.schema_guard import reraise_as_schema_error


class ClickNoticeSchemaError(ValueError):
    """Raised by ``ClickNoticeEvent.from_dict`` on a malformed payload.

    The GUI process should catch this and degrade gracefully
    (log + drop), per wh-uf54. ``from_dict`` never lets a raw
    ``KeyError`` / ``TypeError`` / ``AttributeError`` escape.
    """


# Required fields whose declared type is ``str``.
_STRING_FIELDS = ("outcome", "spoken_name", "app_friendly_name", "trace_id")

# Required fields whose declared type is ``str | None``: present, but
# either a string or None.
_OPTIONAL_STRING_FIELDS = ("reason", "matched_name", "snapshot_id")

# Closed-set membership for the one Literal field in the v5 contract
# ("Click notice IPC schema"):
#   outcome: Literal["not_found", "ambiguous", "execution_failed"]
# ``reason`` is deliberately NOT constrained -- it is an open executor
# tag set, matching the sibling click_element.py which leaves its own
# ``reason`` unconstrained. Note "ok" is NOT a member: a successful
# click produces no notice.
_ALLOWED_OUTCOME = frozenset({"not_found", "ambiguous", "execution_failed"})


@dataclass(frozen=True)
class ClickNoticeEvent:
    """Structured Logic -> GUI notice for a click_element non-ok outcome."""

    outcome: str
    reason: str | None
    matched_name: str | None
    matched_names: tuple[str, ...]
    spoken_name: str
    app_friendly_name: str
    snapshot_id: str | None
    trace_id: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        ``matched_names`` is emitted as a tuple (round-trips through the
        list normalization in ``from_dict``). The exact inverse of
        ``from_dict`` for every field.
        """

        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "matched_name": self.matched_name,
            "matched_names": self.matched_names,
            "spoken_name": self.spoken_name,
            "app_friendly_name": self.app_friendly_name,
            "snapshot_id": self.snapshot_id,
            "trace_id": self.trace_id,
        }

    @classmethod
    @reraise_as_schema_error(ClickNoticeSchemaError)
    def from_dict(cls, payload: Any) -> "ClickNoticeEvent":
        """Parse and validate a wire-format dict.

        Raises ``ClickNoticeSchemaError`` on any structural problem: not
        a mapping, missing required field, wrong field type, an
        ``outcome`` value outside its closed set, or a non-string member
        of ``matched_names``. ``matched_names`` is normalized to a tuple,
        so a sender that uses a list (e.g. via a JSON-bridged transport)
        is accepted.
        """

        if not isinstance(payload, Mapping):
            raise ClickNoticeSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        for name in _STRING_FIELDS:
            if name not in payload:
                raise ClickNoticeSchemaError(
                    f"payload missing required field {name!r}"
                )
            value = payload[name]
            if not isinstance(value, str):
                raise ClickNoticeSchemaError(
                    f"field {name!r} must be a str, got {type(value).__name__}"
                )

        # Closed-set membership for the outcome Literal (v5 contract).
        if payload["outcome"] not in _ALLOWED_OUTCOME:
            raise ClickNoticeSchemaError(
                f"field 'outcome' must be one of {sorted(_ALLOWED_OUTCOME)}, "
                f"got {payload['outcome']!r}"
            )

        for name in _OPTIONAL_STRING_FIELDS:
            if name not in payload:
                raise ClickNoticeSchemaError(
                    f"payload missing required field {name!r}"
                )
            value = payload[name]
            if value is not None and not isinstance(value, str):
                raise ClickNoticeSchemaError(
                    f"field {name!r} must be a str or None, "
                    f"got {type(value).__name__}"
                )

        if "matched_names" not in payload:
            raise ClickNoticeSchemaError(
                "payload missing required field 'matched_names'"
            )
        matched_names = _parse_matched_names(payload["matched_names"])

        return cls(
            outcome=payload["outcome"],
            reason=payload["reason"],
            matched_name=payload["matched_name"],
            matched_names=matched_names,
            spoken_name=payload["spoken_name"],
            app_friendly_name=payload["app_friendly_name"],
            snapshot_id=payload["snapshot_id"],
            trace_id=payload["trace_id"],
        )


def _parse_matched_names(raw: Any) -> tuple[str, ...]:
    """Validate and normalize ``matched_names`` to a tuple of str.

    Restricts the accepted shapes to the EXACT builtin ``list`` or
    ``tuple`` type so iteration is safe. An ``isinstance`` check would
    pass a hostile ``list`` subclass whose ``__iter__`` / ``__len__`` /
    ``__getitem__`` raises, leaking a non-schema exception past the
    wh-uf54 graceful-degrade boundary; the exact-type gate rejects the
    subclass before any such dunder runs. The documented wire shapes --
    a tuple from ``to_dict`` and a list from a JSON-bridged transport --
    both pass. (Mirrors ``click_element.py``.)
    """

    if type(raw) is not list and type(raw) is not tuple:
        raise ClickNoticeSchemaError(
            "field 'matched_names' must be a builtin list or tuple of str, "
            f"got {type(raw).__name__}"
        )
    names: list[str] = []
    for member in raw:
        if not isinstance(member, str):
            raise ClickNoticeSchemaError(
                "field 'matched_names' contains non-string member: "
                f"{type(member).__name__}"
            )
        names.append(member)
    return tuple(names)
