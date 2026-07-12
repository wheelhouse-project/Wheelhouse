"""Bounded TTL set of consumed correlation_tokens (wh-82lnx).

The Logic process tracks which Try-it-anyway correlation_tokens have
already produced a verified-retry signal so a duplicate click on the
same token does not double-increment the per-tuple counter.

The set mirrors the TTL semantics of ``RejectionTokenCache`` (60 second
TTL, 100 entry maximum, oldest-first eviction). It is intentionally a
separate class because the rejection cache stores a tuple value while
this set carries no payload; trying to share state would couple two
unrelated lifecycles.

Thread-safety: not thread-safe. The set is owned by the Logic process
and is driven from the same single-thread asyncio event loop that
serialises GuiManager IPC handling.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable, Optional


_DEFAULT_TTL_SECONDS = 60.0
_DEFAULT_MAX_ENTRIES = 100


class ConsumedTokenSet:
    """Bounded TTL set of consumed correlation_tokens.

    ``add(token)`` records the token (no-op if already present and
    fresh). ``__contains__(token)`` returns True only if the token was
    added within the TTL window; expired entries are pruned during the
    membership check. Eviction on overflow is oldest-first.
    """

    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        time_source: Optional[Callable[[], float]] = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be > 0, got {ttl_seconds}")
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._time: Callable[[], float] = time_source or time.monotonic
        self._entries: "OrderedDict[str, float]" = OrderedDict()

    def add(self, token: str) -> None:
        now = self._time()
        if token in self._entries:
            del self._entries[token]
        self._entries[token] = now
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def __contains__(self, token: str) -> bool:
        stored_at = self._entries.get(token)
        if stored_at is None:
            return False
        if self._time() - stored_at >= self.ttl_seconds:
            del self._entries[token]
            return False
        return True

    def __len__(self) -> int:
        return len(self._entries)
