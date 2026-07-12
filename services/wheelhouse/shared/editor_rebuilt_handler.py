"""Logic-side handler for the editor_rebuilt notification (wh-g2-refactor.17).

When the GUI's ``PersistentEditorRebuilder`` retires an editor
generation, it enqueues an ``editor_rebuilt`` notification onto
``commands_to_logic_queue``. Logic consumes the notification, updates
its observed generation counter, and fans out failures to every
pending ``insert_editor_word`` / ``retract_editor_text`` future whose
stored generation is at or below the retired generation. Section 6 of
``docs/design/2026-05-20-g2-refactor-design-refinements.md`` is the
authoritative reference; this module implements the
``_handle_editor_rebuilt`` pseudocode shown there.

The handler is Qt-free and depends only on the shared schema modules
and ``EditorPendingRequestMap``, so the LogicController integration
(slice 6 / ``wh-g2-refactor.18``) can drop it in behind a single
``handler.handle_notification(payload)`` call from its
``commands_to_logic_queue`` dispatcher.

``REBUILD_LOST_PAYLOAD`` is the canonical fan-out payload. The two
pending maps share a single payload because the producers' post-await
branches read disjoint subsets of the keys (insert reads
``chars_inserted`` and ``failure_reason``; retract reads
``chars_requested``, ``chars_removed``, ``replay_chars``, and
``failure_reason``). A single dict that carries every key satisfies
both producers. The retract producer's boundary-mismatch check
treats ``chars_requested == -1`` as a sentinel that signals the
request was abandoned, so the synthetic payload sets that value to
skip the success-path comparison.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from services.wheelhouse.shared.editor_pending_request import (
    EditorPendingRequestMap,
)
from services.wheelhouse.shared.editor_rebuilt import (
    EditorRebuiltNotification,
    EditorRebuiltSchemaError,
)
from services.wheelhouse.shared.insert_editor_word import (
    FAILURE_EDITOR_REBUILT as INSERT_FAILURE_EDITOR_REBUILT,
    FAILURE_STALE_GENERATION as INSERT_FAILURE_STALE_GENERATION,
)
from services.wheelhouse.shared.retract_editor_text import (
    FAILURE_EDITOR_REBUILT as RETRACT_FAILURE_EDITOR_REBUILT,
)


logger = logging.getLogger(__name__)


# Both schemas declare the same constant value. Asserted explicitly so
# a drift in either schema surfaces at import time rather than as a
# silent failure_reason mismatch at runtime.
assert INSERT_FAILURE_EDITOR_REBUILT == RETRACT_FAILURE_EDITOR_REBUILT == "editor_rebuilt"


def build_rebuild_lost_payload() -> dict[str, Any]:
    """Return the canonical fan-out payload for the rebuild fence.

    See the module docstring for the design rationale. The dict is
    rebuilt on every call so callers cannot accidentally share mutable
    state through a module-level constant.

    Slice 6 producer-side contract (wh-g2-refactor.29.3, gemini round 1
    finding): the retract response handler in ``main.py`` MUST detect
    the ``chars_requested == -1`` sentinel and treat it as the
    rebuild-abandonment path BEFORE running the normal
    ``response["chars_requested"] != chars_requested`` boundary check.
    Without the short-circuit, every rebuild-abandoned retract would
    log a spurious "chars_requested mismatch" and return without
    recognising the rebuild fence. Section 2 of
    ``docs/design/2026-05-20-g2-refactor-design-refinements.md``
    documents the short-circuit on the producer side.
    """
    return {
        # Insert-producer keys.
        "chars_inserted": 0,
        # Retract-producer keys. chars_requested = -1 is the abandon-
        # path sentinel allowed by RetractEditorTextResponse's
        # ``minimum=-1`` validator: the retract producer's post-await
        # boundary check treats it as "request was abandoned, skip the
        # mismatch comparison". See the slice 6 producer-side contract
        # in the docstring above.
        "chars_requested": -1,
        "chars_removed": 0,
        "replay_chars": 0,
        # Shared key. Both producers branch on this value.
        "failure_reason": INSERT_FAILURE_EDITOR_REBUILT,
    }


class LogicRebuildFanout:
    """Logic-side ``editor_rebuilt`` handler.

    Owns the observed-generation counter and the list of pending-
    request maps to fan out across. The integration in
    ``wheelhouse/main.py`` (slice 6) constructs one instance with the
    insert pending map AND the retract pending map registered, then
    calls ``handle_notification(payload)`` from the
    ``commands_to_logic_queue`` dispatcher branch for action
    ``editor_rebuilt``.

    The class is NOT thread-safe; it must be driven from the
    LogicController's asyncio event loop only (matches the
    single-thread invariant ``EditorPendingRequestMap`` already
    relies on).
    """

    def __init__(
        self,
        *,
        pending_maps: Iterable[EditorPendingRequestMap],
        initial_generation: int = 0,
    ) -> None:
        self._pending_maps: list[EditorPendingRequestMap] = list(pending_maps)
        self._observed_generation = initial_generation

    @property
    def observed_generation(self) -> int:
        """Return the highest editor generation Logic has observed."""
        return self._observed_generation

    def handle_notification(self, payload: Any) -> bool:
        """Process an ``editor_rebuilt`` notification.

        Returns ``True`` if the notification was applied; ``False`` if
        the payload was malformed (graceful degradation per wh-uf54) or
        the notification's ``new_generation`` does not advance
        ``observed_generation`` -- which both blocks a duplicate
        delivery of the same notification and quietly absorbs an
        out-of-order older notification whose work has already been
        covered by a later notification's fan-out.

        Side effects on success:

          * Bumps ``observed_generation`` to ``new_generation``.
          * For each registered pending map, calls
            ``fail_at_or_below(old_generation=...)`` with the canonical
            RebuildLost payload. Logs the per-map abandon count.

        Round 1 / gemini finding wh-g2-refactor.29.1: dedup is now
        keyed on ``new_generation > observed_generation`` rather than
        on a separate ``set`` of accepted ``(old, new)`` pairs. The set
        was redundant -- ``fail_at_or_below`` is itself idempotent
        and the generation counter is the simpler bounded signal --
        and it would have grown without limit over the Logic process's
        lifetime.
        """
        try:
            notification = EditorRebuiltNotification.from_dict(payload)
        except EditorRebuiltSchemaError as exc:
            logger.warning(
                "Dropping malformed editor_rebuilt notification: %s", exc,
            )
            return False

        if notification.new_generation <= self._observed_generation:
            logger.warning(
                "Duplicate or stale editor_rebuilt notification "
                "(old=%d, new=%d, observed=%d); ignoring",
                notification.old_generation,
                notification.new_generation,
                self._observed_generation,
            )
            return False

        logger.info(
            "editor_rebuilt old=%d new=%d reason=%s; failing pending futures",
            notification.old_generation,
            notification.new_generation,
            notification.reason,
        )

        self._observed_generation = notification.new_generation

        failure_payload = build_rebuild_lost_payload()
        total_abandoned = 0
        for index, pending in enumerate(self._pending_maps):
            abandoned = pending.fail_at_or_below(
                old_generation=notification.old_generation,
                failure_payload=failure_payload,
            )
            total_abandoned += len(abandoned)
            logger.info(
                "editor_rebuilt fan-out: map=%d abandoned=%d",
                index, len(abandoned),
            )

        logger.info(
            "editor_rebuilt fan-out total: abandoned=%d", total_abandoned,
        )
        return True

    def is_rebuild_lost(self, payload: Any) -> bool:
        """Return True if the payload matches the rebuild-lost shape.

        Producers MAY call this from their post-await branch to
        distinguish a rebuild abandonment from a normal failure. The
        check matches both rebuild fences documented in Section 6:

          * ``"editor_rebuilt"`` -- bulk fan-out from the Logic-side
            ``editor_rebuilt`` notification handler.
          * ``"stale_generation"`` -- per-request rejection by the GUI
            dispatcher when the request's editor_generation does not
            match the live editor's counter.

        Both are semantically equivalent at the producer level (drop
        the word, do not retry, the next STT update will heal the
        document). The two reasons differ only in source, so a
        producer using ``is_rebuild_lost`` as the rebuild-abandonment
        gate must see both. Round 1 / deepseek finding
        wh-g2-refactor.30.1: matching only ``editor_rebuilt`` made
        ``is_rebuild_lost`` asymmetric with
        ``RebuildLost.from_payload``, which already accepts both.
        """
        if not isinstance(payload, Mapping):
            return False
        return payload.get("failure_reason") in (
            INSERT_FAILURE_EDITOR_REBUILT,
            INSERT_FAILURE_STALE_GENERATION,
        )
