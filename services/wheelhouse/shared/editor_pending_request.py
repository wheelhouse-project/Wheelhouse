"""Logic-side pending-request map for editor IPC (wh-g2-refactor.14).

Backs both ``insert_editor_word`` and ``retract_editor_text``. Each
outbound request registers a future keyed by ``request_id``; the
matching inbound response resolves the future via ``complete``. The
producer's ``finally`` block calls ``pop`` to clean up regardless of
whether the await succeeded or timed out.

The map stores a ``(future, generation)`` tuple so the rebuild fan-out
(Section 6) can identify and fail futures whose stored generation is
at or below the retired editor generation. ``fail_at_or_below`` is the
fan-out hook; it resolves stale futures with a caller-supplied payload
but deliberately does NOT pop entries: late responses from the old
editor still find the future already done and follow the existing
late-response path.

Design references:

  * ``docs/design/2026-05-20-g2-refactor-design-refinements.md``
    Section 2 (retract pending map),
    Section 5 (insert pending map),
    Section 6 (rebuild fan-out via fail_at_or_below).
  * ``services/wheelhouse/app.py`` ``WheelHouseApp.send_request``
    is the upstream pattern: one process boundary downstream, the
    same shape (uuid4 request_id, per-request future, await with
    timeout, finally pop). This module mirrors that contract on the
    Logic side for the GUI-process boundary.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional


logger = logging.getLogger(__name__)


class EditorPendingRequestMap:
    """Pending-request map for the Logic <-> GUI editor IPC.

    Keys are ``request_id`` strings (uuid4 hex). Values are
    ``(asyncio.Future, generation)`` tuples. The class is NOT
    thread-safe; it is owned by the LogicController's asyncio event
    loop and must be driven from that loop only (matches the
    single-thread invariant the existing ``app.response_futures``
    pattern relies on at app.py:425-427).
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[asyncio.Future[Any], int]] = {}

    def register(self, request_id: str, *, generation: int) -> asyncio.Future[Any]:
        """Register a pending request and return the future to await.

        Raises ``ValueError`` on a duplicate request_id. uuid4
        collisions are not expected in practice; a duplicate id almost
        certainly indicates a producer bug (id reuse), and surfacing
        it loudly is better than silently dropping the first future.

        Producer contract: the caller MUST place ``register()`` inside
        the same ``try`` whose ``finally`` calls ``pop(request_id)``.
        If an exception fires between ``register()`` and the ``try``
        (e.g. a ``state_to_gui_queue.put_nowait`` that raises on a full
        queue), no ``finally`` runs and the entry leaks permanently --
        ``in_flight()`` never returns to zero. Correct shape::

            future = pending.register(rid, generation=gen)
            try:
                queue.put_nowait(request_dict)
                response = await asyncio.wait_for(future, timeout=2.0)
            finally:
                pending.pop(rid)
        """
        if request_id in self._entries:
            raise ValueError(
                f"duplicate request_id {request_id!r}; producer reused id"
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._entries[request_id] = (future, generation)
        return future

    def complete(self, request_id: str, payload: Any) -> bool:
        """Resolve the future for request_id with ``payload``.

        Returns ``True`` if the future was found and not yet done;
        ``False`` if the request_id was unknown OR the future was
        already resolved (e.g. by a rebuild fan-out or by a previous
        complete on a duplicate response).

        The handler does NOT pop the entry on completion. The
        producer's ``finally`` block is the single owner of pop, so
        a late response that arrives after timeout-driven pop hits
        the unknown-id branch and returns ``False`` (the existing
        late-response semantics).
        """
        entry = self._entries.get(request_id)
        if entry is None:
            return False
        future, _generation = entry
        if future.done():
            return False
        future.set_result(payload)
        return True

    def pop(self, request_id: str) -> bool:
        """Drop the entry for ``request_id``. Returns whether one was present.

        Called from the producer's ``finally`` block after the
        ``asyncio.wait_for`` returns or raises.
        """
        return self._entries.pop(request_id, None) is not None

    def get_generation(self, request_id: str) -> Optional[int]:
        """Return the generation a request was stamped with, or None."""
        entry = self._entries.get(request_id)
        if entry is None:
            return None
        return entry[1]

    def in_flight(self) -> int:
        """Return the number of entries currently in the map.

        Note that ``complete`` does NOT pop entries; ``in_flight``
        counts every entry whether its future is resolved or not.
        The producer's ``finally`` pop is what removes the entry.
        """
        return len(self._entries)

    def fail_at_or_below(
        self,
        *,
        old_generation: int,
        failure_payload: Any,
    ) -> list[str]:
        """Fan-out failure for the rebuild fence (Section 6).

        Resolves every stored future whose stored generation is at or
        below ``old_generation`` with ``failure_payload``. Returns the
        list of request_ids that were abandoned (for log accounting).

        Skips futures that are already done -- the resolve loses to
        whoever completed first, and ``complete`` already returned
        ``False`` for them, so there is no double-resolve.

        Does NOT pop entries. The producers' ``finally`` blocks are
        the single owner of pop. A late response from the old editor
        still finds the future already done and follows the
        existing late-response path.

        ``failure_payload`` contract: this map holds futures from BOTH
        the insert and retract producers, so a single call to
        ``fail_at_or_below`` resolves futures of both shapes with the
        SAME payload. The payload must therefore carry every key both
        producers' post-await logic reads. With the current schemas:

            * insert producer reads ``chars_inserted`` and ``failure_reason``
            * retract producer reads ``chars_requested``, ``chars_removed``,
              ``replay_chars``, and ``failure_reason``

        The minimum-viable rebuild fan-out payload is therefore::

            {
                "chars_inserted": 0,
                "chars_requested": -1,
                "chars_removed": 0,
                "replay_chars": "",
                "failure_reason": FAILURE_EDITOR_REBUILT,
            }

        ``chars_requested = -1`` exploits the
        ``RetractEditorTextResponse.chars_requested >= -1`` allowance
        as a sentinel that tells the retract producer's mismatch-log
        to skip the boundary-mismatch check. Any future schema change
        that adds a new required field to either response MUST also
        update the rebuild handler that calls ``fail_at_or_below``;
        the producers' post-await ``response["new_field"]`` access
        would otherwise raise ``KeyError`` on the synthetic payload.
        """
        abandoned: list[str] = []
        for request_id, (future, generation) in self._entries.items():
            if generation > old_generation:
                continue
            if future.done():
                continue
            future.set_result(failure_payload)
            abandoned.append(request_id)
        return abandoned
