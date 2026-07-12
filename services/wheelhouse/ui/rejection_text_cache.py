"""Bounded TTL cache mapping correlation_token to dictation text (wh-7318z).

The Input process owns this cache. When RejectedInsertionStrategy
emits a text_target_rejected event (wh-9weum, Phase 2), it stores the
token -> original_text pairing here so the optional Phase 4 retry
click (wh-ftg63) can recover the text without it crossing process
boundaries. The cache is bounded in size and time so a busy session
does not grow without limit and a stale token cannot resurrect old
dictation.

Privacy contract (per wh-x4mv.2 round 2):
  * The Input process holds the token -> text cache (this class).
  * The Logic process holds a separate token -> tuple cache.
  * Text never enters the Logic or GUI process.

Thread-safety: not thread-safe. The cache is owned by the Input
process and accessed only on the input main loop.

Lookup outcomes (wh-9weum.1.4):
  * HIT     -- token is known and alive. text is set.
  * EXPIRED -- token was stored but its TTL elapsed; the entry has
               been pruned from the cache.
  * MISS    -- token was never stored, or was previously evicted by
               max_entries pressure.

The two non-hit outcomes map to the retry_dictation_by_token contract's
``token_expired`` and ``unknown_token`` statuses (wh-wt82). The
distinction lets log surfaces tell a stale-cache miss from an
out-of-cache miss.

TTL invariant (wh-override-multiword-retry): the multi-word aggregation
on RejectedInsertionStrategy relies on cache entries surviving for at
least as long as the GUI rejection toast's suppression cooldown
(``rejection_rate_limit.ToastSuppressionMap._DEFAULT_COOLDOWN_SECONDS``,
60s). Both this cache's default TTL and the cooldown default are 60s
today, and the cache TTL is sliding (every successful ``put`` resets
the entry's stored_at), so continuous dictation keeps the entry alive
well past the fixed cooldown. If either default is reduced without
the other, multi-word utterances on rejected targets will lose words
between aggregation boundaries: the GUI's ``_last_rejection_token``
update on every event (per wh-vbvgf.3.1) keeps pointing the visible
Try-it-anyway button at the newest token, but the prior cache entry
will have expired before the cooldown lifted, so the user's click
replays only the words that arrived after the gap.
"""

from __future__ import annotations

import enum
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional


_DEFAULT_TTL_SECONDS = 60.0
_DEFAULT_MAX_ENTRIES = 100


class CacheStatus(enum.Enum):
    """Outcome of a cache lookup (wh-9weum.1.4)."""

    HIT = "hit"
    EXPIRED = "expired"
    MISS = "miss"


@dataclass(frozen=True)
class CacheResult:
    """Result of a RejectionTextCache lookup.

    ``text`` is set only when ``status == HIT``; it is None for
    EXPIRED and MISS.

    ``target_hwnd`` is the top-level window handle that had focus when
    the rejection event was emitted. Stored only on HIT entries; 0 on
    EXPIRED and MISS results. The retry handler restores foreground to
    this HWND before running ClipboardOnlyStrategy so the paste lands
    on the originally-rejected window rather than the toast button the
    user clicked. See wh-override-paste-focus-drift.
    """

    status: CacheStatus
    text: Optional[str] = None
    target_hwnd: int = 0
    target_process_id: int = 0


_MISS = CacheResult(CacheStatus.MISS, None)


class RejectionTextCache:
    """Token -> original-dictation-text cache with TTL and bounded size."""

    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        time_source: Optional[Callable[[], float]] = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._time_source: Callable[[], float] = time_source or time.monotonic
        # OrderedDict gives us O(1) eviction of the oldest entry on
        # overflow. Insertion order matches the order we want to evict
        # in: oldest first. Entry shape:
        # (text, target_hwnd, target_process_id, stored_at).
        self._entries: (
            "OrderedDict[str, tuple[str, int, int, float]]"
        ) = OrderedDict()

    def put(
        self, token: str, text: str,
        target_hwnd: int = 0, target_process_id: int = 0,
    ) -> None:
        """Store ``text`` under ``token``. Replaces any previous value.

        Replacing an existing entry resets its expiry. If inserting
        would push the cache past ``max_entries``, the oldest entry is
        evicted.

        ``target_hwnd`` is the top-level window handle that had focus
        when the rejection was emitted. The retry handler reads it
        back to restore foreground to the original window before
        pasting (wh-override-paste-focus-drift). Defaults to 0, which
        the retry handler treats as 'no refocus needed' so legacy
        callers that omit the argument continue to work.

        ``target_process_id`` is the OS process ID owning the
        focused control at rejection time. The retry handler uses
        it to detect HWND reuse: Windows can reassign an HWND to a
        new window if the original closes within the cache TTL.
        Validating that the live HWND's PID still matches the
        cached PID prevents the cached dictation from leaking to an
        unrelated window (wh-override-paste-focus-drift.1.2).
        Defaults to 0, which the retry handler treats as 'no PID
        recorded -- skip the identity check'.

        wh-override-multiword-retry: the assignment is exception-safe
        by construction. We assign the new tuple first (single dict
        op, atomic in CPython), then move the key to the end of the
        OrderedDict to reset eviction priority. If ``move_to_end``
        raises, the entry's value is already updated; only the
        eviction position is stale, which corrects itself on the next
        ``put`` against the same token. The earlier ``del`` + assign
        pattern could destroy the previous entry if the second
        statement raised, which the multi-word aggregation path on
        RejectedInsertionStrategy relies on never happening.
        """

        now = self._time_source()
        self._entries[token] = (text, target_hwnd, target_process_id, now)
        self._entries.move_to_end(token, last=True)
        # Evict oldest entries until we are within max_entries.
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def invalidate(self, token: str) -> bool:
        """Drop ``token``'s entry from the cache.

        Returns True if the token was present and removed, False if it
        was not in the cache. Idempotent and safe to call on unknown
        tokens.

        wh-override-multiword-retry: called by the retry handler after
        a verified Try-it-anyway click so the next user-spoken
        dictation against the same target allocates a fresh
        correlation_token instead of appending onto an already-consumed
        entry. Without this, the aggregation map on
        RejectedInsertionStrategy would keep extending the entry whose
        Logic-side counterpart has been recorded in
        ``consumed_retry_tokens``, and the user's next click on the
        same toast would be silently dropped by the duplicate-click
        short-circuit.
        """

        if token in self._entries:
            del self._entries[token]
            return True
        return False

    def resolve(self, token: str) -> CacheResult:
        """Look up ``token`` and return a three-way outcome.

        Returns:
          * CacheResult(HIT, text, target_hwnd) -- token alive.
          * CacheResult(EXPIRED, None, 0) -- token was stored but TTL
            elapsed. The entry is pruned before this method returns.
          * CacheResult(MISS, None, 0) -- token was never stored or
            was evicted under max_entries pressure.

        This is the canonical lookup. ``get`` is preserved for callers
        that only need the text; new callers should prefer ``resolve``
        so they can map the three outcomes to the
        retry_dictation_by_token statuses.
        """

        entry = self._entries.get(token)
        if entry is None:
            return _MISS
        text, target_hwnd, target_process_id, stored_at = entry
        if self._time_source() - stored_at >= self.ttl_seconds:
            del self._entries[token]
            return CacheResult(CacheStatus.EXPIRED, None, 0, 0)
        return CacheResult(
            CacheStatus.HIT, text, target_hwnd, target_process_id,
        )

    def get(self, token: str) -> Optional[str]:
        """Return the text for ``token`` if alive, else None.

        Convenience wrapper over ``resolve`` that collapses EXPIRED
        and MISS into a single None. Existing callers and legacy
        tests use this; new callers that need to distinguish the
        two outcomes should call ``resolve`` directly.
        """

        result = self.resolve(token)
        return result.text if result.status is CacheStatus.HIT else None

    def keys(self) -> list[str]:
        """Return a list of currently-active (non-expired) tokens.

        Used by tests and by diagnostic surfaces. Has the side effect
        of pruning all expired entries it walks past.
        """

        now = self._time_source()
        expired = [
            token
            for token, (_text, _hwnd, _pid, stored_at) in self._entries.items()
            if now - stored_at >= self.ttl_seconds
        ]
        for token in expired:
            del self._entries[token]
        return list(self._entries.keys())
