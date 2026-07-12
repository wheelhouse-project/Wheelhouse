"""Boundary-validation helper for new IPC schemas (wh-uf54).

The wh-9weum (text-target gate relaxation) Phase 2 and Phase 4
consumers receive new structured IPC messages at process boundaries.
A malformed payload must not unwind into the message loop -- it
should be logged and dropped so the receiver keeps running.

Usage at a boundary (Logic process, ``_handle_input_event``):

    from services.wheelhouse.shared.ipc_schema_validation import safe_parse
    from services.wheelhouse.shared.text_target_rejection import (
        TextTargetRejectedEvent,
    )

    event = safe_parse(
        TextTargetRejectedEvent.from_dict,
        msg,
        log_label="text_target_rejected",
    )
    if event is None:
        return  # already logged
    # ... normal handling

The helper catches any ``ValueError`` raised by the parser. Both
``TextTargetRejectedSchemaError`` and
``RetryDictationByTokenSchemaError`` derive from ``ValueError``, so
the helper handles both. A non-ValueError exception (for example a
``RuntimeError`` because of a bug in the parser itself) is allowed
to propagate -- swallowing it would hide a real bug.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def safe_parse(
    parser: Callable[[Any], T],
    payload: Any,
    *,
    log_label: str,
) -> Optional[T]:
    """Parse ``payload`` via ``parser``; on a schema error, log and drop.

    Returns the parsed object on success. Returns ``None`` and emits
    a WARNING log line carrying ``log_label`` and the parser's error
    message when the parser raises ``ValueError`` (which covers the
    ``*SchemaError`` subclasses defined in this package).

    Non-``ValueError`` exceptions propagate unchanged.

    ``log_label`` should be the message-type or action name, so a
    real consumer can distinguish which boundary produced the
    warning when several use this helper.
    """

    try:
        return parser(payload)
    except ValueError as exc:
        logger.warning(
            "Dropping malformed %s payload: %s", log_label, exc
        )
        return None
