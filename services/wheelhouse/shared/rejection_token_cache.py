"""Logic-side bounded TTL cache mapping correlation_token to rejection tuple
(wh-iycks).

The Logic process owns this cache. When ``LogicController`` receives a
``text_target_rejected`` event from the Input process, it stores the
event's identifying tuple under the event's ``correlation_token``. When
the user later clicks "Try it anyway", the GUI process emits a
``try_anyway_clicked`` event carrying only the correlation_token; the
Logic-side handler resolves the token in this cache to decide whether
to forward the retry request to Input or to surface the click-too-late
follow-up toast.

This cache is parallel to ``ui.rejection_text_cache.RejectionTextCache``
on the Input side. The two caches deliberately split responsibilities
under the wh-x4mv.2 round-2 privacy contract:

  * Input owns ``correlation_token -> original_text`` -- the dictation
    text never crosses process boundaries.
  * Logic owns ``correlation_token -> rejection tuple`` -- the
    identifying fields needed by future bd children (wh-82lnx
    verified-retry counter, wh-bqv9c three-strikes persistence) but
    not the dictation text.

Privacy contract: no field in :class:`RejectionTuple` carries
dictation text. ``process_name``, ``class_name``, ``control_type``,
and ``app_friendly_name`` come straight from the rejection event
schema (services/wheelhouse/shared/text_target_rejection.py); none of
them is text the user spoke.

Thread-safety: not thread-safe. The cache is owned by the Logic
process and is driven from the same single-thread asyncio event loop
that handles GuiManager IPC. See architecture-overview.md
"Concurrency" notes for the broader contract.

Lookup outcomes mirror the Input-side cache (wh-9weum.1.4):

  * HIT     -- token is known and alive. ``tuple_`` is set.
  * EXPIRED -- token was stored but its TTL elapsed. The entry is
               pruned from the cache before resolve() returns.
  * MISS    -- token was never stored, or was previously evicted by
               max_entries pressure.

The handler in :mod:`main.py` collapses EXPIRED and MISS into a single
"click_too_late" outcome (no IPC to Input, follow-up toast); the
distinction is preserved in the cache surface so log lines can tell
the two apart.
"""

from __future__ import annotations

import enum
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional


_DEFAULT_TTL_SECONDS = 60.0
_DEFAULT_MAX_ENTRIES = 100


@dataclass(frozen=True)
class RejectionTuple:
    """Identifying tuple stored in the Logic-side rejection cache.

    Mirrors the subset of :class:`TextTargetRejectedEvent` fields that
    the Phase 4 retry pipeline and follow-up beads need:

      * ``process_name`` / ``class_name`` / ``control_type`` -- the
        three fields wh-82lnx (counter) and wh-bqv9c (three-strikes)
        will key off.
      * ``app_friendly_name`` -- carried for display surfaces (e.g. a
        future repeated-failure dialog) so the consumer does not have
        to re-resolve the executable's friendly name.

    The dictation text is NOT in this tuple, by design.
    """

    process_name: str
    class_name: str
    control_type: str
    app_friendly_name: str


class CacheStatus(enum.Enum):
    """Outcome of a RejectionTokenCache lookup."""

    HIT = "hit"
    EXPIRED = "expired"
    MISS = "miss"


@dataclass(frozen=True)
class CacheResult:
    """Result of a :meth:`RejectionTokenCache.resolve` call.

    ``tuple_`` is set only when ``status == HIT``; it is None for
    EXPIRED and MISS. The trailing underscore avoids shadowing the
    builtin ``tuple``.
    """

    status: CacheStatus
    tuple_: Optional[RejectionTuple] = None


_MISS = CacheResult(CacheStatus.MISS, None)


class RejectionTokenCache:
    """Token -> RejectionTuple cache with TTL and bounded size."""

    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        time_source: Optional[Callable[[], float]] = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._time_source: Callable[[], float] = time_source or time.monotonic
        # OrderedDict gives O(1) eviction of the oldest entry on
        # overflow. Insertion order matches the order we want to evict
        # in: oldest first.
        self._entries: "OrderedDict[str, tuple[RejectionTuple, float]]" = (
            OrderedDict()
        )

    def put(self, token: str, value: RejectionTuple) -> None:
        """Store ``value`` under ``token``. Replaces any previous value.

        Replacing an existing entry resets its expiry. If inserting
        would push the cache past ``max_entries``, the oldest entry is
        evicted.
        """

        now = self._time_source()
        if token in self._entries:
            del self._entries[token]
        self._entries[token] = (value, now)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def resolve(self, token: str) -> CacheResult:
        """Look up ``token`` and return a three-way outcome.

        Returns:
          * CacheResult(HIT, tuple_)    -- token alive.
          * CacheResult(EXPIRED, None)  -- token was stored but TTL
            elapsed. The entry is pruned before this method returns.
          * CacheResult(MISS, None)     -- token was never stored or
            was evicted under max_entries pressure.
        """

        entry = self._entries.get(token)
        if entry is None:
            return _MISS
        value, stored_at = entry
        if self._time_source() - stored_at >= self.ttl_seconds:
            del self._entries[token]
            return CacheResult(CacheStatus.EXPIRED, None)
        return CacheResult(CacheStatus.HIT, value)

    def get(self, token: str) -> Optional[RejectionTuple]:
        """Return the tuple for ``token`` if alive, else None.

        Convenience wrapper over :meth:`resolve` that collapses EXPIRED
        and MISS into a single None. Mirrors the input-side
        ``RejectionTextCache.get`` shape so callers that do not need to
        distinguish stale-cache from never-stored can stay terse.
        """

        result = self.resolve(token)
        return result.tuple_ if result.status is CacheStatus.HIT else None

    def keys(self) -> list[str]:
        """Return a list of currently-active (non-expired) tokens.

        Has the side effect of pruning all expired entries it walks
        past. Used by tests and diagnostic surfaces.
        """

        now = self._time_source()
        expired = [
            token
            for token, (_value, stored_at) in self._entries.items()
            if now - stored_at >= self.ttl_seconds
        ]
        for token in expired:
            del self._entries[token]
        return list(self._entries.keys())
