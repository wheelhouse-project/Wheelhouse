"""Correlation-token validation shared by the wh-9weum (text-target gate
relaxation and override toast) IPC schemas.

The text_target_rejected event (wh-hqipv) and the retry_dictation_by_token
request (wh-wt82) both carry a correlation_token that the bead text
specified as a uuid4 string. The bead text is the privacy contract:
the token is opaque, used only for round-trip correlation between the
rejection event and the optional Phase 4 retry click. A producer that
puts arbitrary content into ``correlation_token`` would defeat that
opacity, and the schemas should reject that at the boundary instead of
forwarding non-token strings under the trusted token field
(wh-9weum.1.3).

Validators raise an exception class chosen by the caller so each
schema's own ``*SchemaError`` derives unchanged from ``ValueError`` and
the safe_parse helper (wh-uf54) keeps catching the same one.
"""

from __future__ import annotations

import uuid
from typing import Any, Type


def validate_correlation_token(
    value: Any,
    *,
    field_name: str,
    error_class: Type[ValueError],
) -> str:
    """Return ``value`` if it is a uuid4-shaped string, else raise.

    ``value`` must be a ``str`` (rejects bytes / int / None). The
    string must parse as a UUID and have ``version == 4``. The
    parsed UUID's canonical form must equal the lowercased input
    string -- this keeps a single canonical wire form so a sender
    cannot smuggle a longer payload into the token field via
    leading/trailing whitespace or non-canonical separators.

    Raises ``error_class`` (a ValueError subclass picked by the
    caller's schema) on any failure. Caller wraps the call in their
    own from_dict / __post_init__.
    """

    if not isinstance(value, str):
        raise error_class(
            f"field {field_name!r} must be a str, got "
            f"{type(value).__name__}"
        )
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise error_class(
            f"field {field_name!r} is not a valid UUID: {exc}"
        )
    if parsed.version != 4:
        raise error_class(
            f"field {field_name!r} must be a uuid4 string, got version {parsed.version}"
        )
    if str(parsed) != value.lower():
        raise error_class(
            f"field {field_name!r} is not in canonical UUID form: {value!r}"
        )
    return value
