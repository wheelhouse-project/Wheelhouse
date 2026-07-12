"""Tests for the rejection-event emission path (wh-7318z).

When the router selects RejectedInsertionStrategy because the
text-target predicate produced a hard reject, the strategy must:

  1. Generate a fresh uuid4 correlation_token.
  2. Store token -> original_text in its input-process cache so the
     optional Phase 4 retry click can recover the text.
  3. Build a structured TextTargetRejectedEvent payload from the
     verdict + UIContext and put it on the response queue with
     ``type=text_target_rejected``.
  4. Continue to return the existing
     InsertionResult(success=True, clipboard_dirty=False,
     rejected_reason=...) so the IPC demuxer continues to work.

Privacy contract:
  * The dictation text MUST NOT appear in the emitted IPC payload.
    Only correlation_token threads the round trip.

Backward compatibility:
  * Constructing RejectedInsertionStrategy() with no args must keep
    working (the existing test fixtures do that). When response_queue
    or cache is None, the strategy does NOT emit -- it behaves
    exactly as it did before wh-7318z.
"""

from __future__ import annotations

import uuid
from queue import Queue
from unittest.mock import MagicMock

from ui.context import UIContext
from ui.rejection_text_cache import RejectionTextCache
from ui.strategies.specific import RejectedInsertionStrategy
from ui.text_perfector import TextPerfector
from ui.text_target import TextTargetVerdict


def _make_context() -> UIContext:
    ctrl = MagicMock()
    ctrl.ControlTypeName = "Pane"
    return UIContext(
        focused_control=ctrl,
        is_flutter=False,
        is_terminal=False,
        process_name="zed.exe",
        class_name="zed::Workspace",
        process_id=12345,
    )


def _make_verdict(
    reason: str = "default_reject_paste_capable_class",
) -> TextTargetVerdict:
    return TextTargetVerdict(
        verdict=False,
        reason=reason,
        supported_patterns=("Invoke",),
        control_type="Pane",
        class_name="zed::Workspace",
        process_name="zed.exe",
    )


# ---------------------------------------------------------------------------
# Backward compatibility: legacy no-arg construction
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_no_arg_construction_still_works(self):
        strategy = RejectedInsertionStrategy()
        result = strategy.insert("hello", _make_context())
        assert result.success is True
        assert result.was_rejected is True
        assert result.rejected_reason == RejectedInsertionStrategy.DEFAULT_REASON

    def test_no_args_means_no_emit(self):
        # When response_queue and cache are None, the strategy MUST NOT
        # raise even if the router calls set_pending_verdict on it.
        strategy = RejectedInsertionStrategy()
        if hasattr(strategy, "set_pending_verdict"):
            strategy.set_pending_verdict(_make_verdict())
        result = strategy.insert("hello", _make_context())
        assert result.success is True


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


class TestEmission:
    def test_emit_payload_carries_msg_type(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello world", _make_context())
        msg = queue.get_nowait()
        assert msg["type"] == "text_target_rejected"

    def test_emit_payload_carries_verdict_fields(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        verdict = _make_verdict(reason="default_reject_paste_capable_class")
        strategy.set_pending_verdict(verdict)
        strategy.insert("hello", _make_context())
        msg = queue.get_nowait()
        assert msg["process_name"] == "zed.exe"
        assert msg["class_name"] == "zed::Workspace"
        assert msg["control_type"] == "Pane"
        assert msg["reason"] == "default_reject_paste_capable_class"
        assert msg["supported_patterns"] == ("Invoke",)

    def test_emit_payload_uses_process_name_when_no_resolver(self):
        # When constructed without an app_name_resolver, the strategy
        # falls back to process_name so older test fixtures and any
        # caller that has not yet wired in the resolver still work.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        msg = queue.get_nowait()
        assert msg["app_friendly_name"] == "zed.exe"

    def test_emit_payload_uses_resolver_when_provided(self):
        # wh-b0sch: when an app_name_resolver is provided the strategy
        # uses its resolved friendly name (FileDescription from the
        # exe's VS_VERSIONINFO).
        queue: Queue = Queue()
        cache = RejectionTextCache()

        class _StubResolver:
            def __init__(self) -> None:
                self.calls: list[tuple[int, str]] = []

            def resolve(self, pid: int, fallback: str) -> str:
                self.calls.append((pid, fallback))
                return "Zed Editor"

        resolver = _StubResolver()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
            app_name_resolver=resolver,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        msg = queue.get_nowait()
        assert msg["app_friendly_name"] == "Zed Editor"
        assert resolver.calls == [(12345, "zed.exe")]

    def test_first_log_map_escalates_to_info_once_per_key(self, caplog):
        # wh-zib65: input-process first-rejection diagnostic log. The
        # strategy must call should_log on the first rejection per
        # (process, class, reason) and emit an INFO line; subsequent
        # rejections for the same key stay at DEBUG.
        import logging as logging_mod
        from rejection_rate_limit import FirstRejectionLogMap

        queue: Queue = Queue()
        cache = RejectionTextCache()
        log_map = FirstRejectionLogMap()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
            first_log_map=log_map,
        )

        # Sanity: the wiring should set the map.
        assert strategy._first_log_map is log_map

        with caplog.at_level(logging_mod.INFO, logger="ui.strategies.specific"):
            strategy.set_pending_verdict(_make_verdict())
            strategy.insert("hello", _make_context())
        first_count = sum(
            1 for r in caplog.records
            if r.levelno == logging_mod.INFO
            and "first per key" in r.message
        )
        assert first_count == 1, (
            f"expected 1 INFO 'first per key' log, got {first_count}; "
            f"records: {[(r.levelname, r.name, r.message) for r in caplog.records]}"
        )

        caplog.clear()
        with caplog.at_level(logging_mod.INFO):
            strategy.set_pending_verdict(_make_verdict())
            strategy.insert("hello", _make_context())
        second_count = sum(
            1 for r in caplog.records
            if r.levelno == logging_mod.INFO
            and "first per key" in r.message
        )
        assert second_count == 0

    def test_no_first_log_map_means_no_info_log(self, caplog):
        # When first_log_map is None (legacy construction), the
        # strategy does NOT emit the INFO log -- only the existing
        # DEBUG line fires.
        import logging as logging_mod

        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        with caplog.at_level(logging_mod.INFO):
            strategy.set_pending_verdict(_make_verdict())
            strategy.insert("hello", _make_context())
        info_count = sum(
            1 for r in caplog.records
            if r.levelno == logging_mod.INFO
            and "first per key" in r.message
        )
        assert info_count == 0

    def test_resolver_exception_falls_back_to_process_name(self):
        # If the resolver raises, the rejection toast must still show
        # something useful; the strategy logs and falls back to the
        # captured process_name.
        queue: Queue = Queue()
        cache = RejectionTextCache()

        class _BrokenResolver:
            def resolve(self, pid: int, fallback: str) -> str:
                raise OSError("access denied")

        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
            app_name_resolver=_BrokenResolver(),
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        msg = queue.get_nowait()
        assert msg["app_friendly_name"] == "zed.exe"

    def test_emit_payload_correlation_token_is_uuid4(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        msg = queue.get_nowait()
        token = msg["correlation_token"]
        parsed = uuid.UUID(token)
        assert parsed.version == 4

    def test_two_rejections_different_keys_produce_distinct_tokens(self):
        # wh-override-multiword-retry: distinct rejection keys still
        # produce distinct tokens. Aggregation only collapses tokens
        # when the rejection key (process, class, control_type, reason)
        # is the same. Build a second verdict whose class_name differs
        # so the suppression key differs but both stay in the uncertain
        # category (default_reject_paste_capable_class).
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        verdict2 = TextTargetVerdict(
            verdict=False,
            reason="default_reject_paste_capable_class",
            supported_patterns=("Invoke",),
            control_type="Pane",
            class_name="OtherEditor::Workspace",
            process_name="zed.exe",
        )
        strategy.set_pending_verdict(verdict2)
        strategy.insert("world", _make_context())
        msg1 = queue.get_nowait()
        msg2 = queue.get_nowait()
        assert msg1["correlation_token"] != msg2["correlation_token"]


# ---------------------------------------------------------------------------
# Multi-word aggregation (wh-override-multiword-retry)
# ---------------------------------------------------------------------------


class TestMultiWordAggregation:
    """Aggregate per-word rejections inside the cooldown window so the
    Try-it-anyway click replays the whole utterance, not just the last
    word the user spoke (wh-override-multiword-retry).
    """

    def test_two_rejections_same_key_share_one_token(self):
        # The speech pipeline emits one stable word at a time. When the
        # user dictates 'hello world' into a soft-rejected target, the
        # strategy receives two insert() calls. Both must point at the
        # same correlation_token so the GUI's last-rejection-token
        # binding (updated on every event per wh-vbvgf.3.1) keeps the
        # visible Try-it-anyway button bound to one cache entry that
        # holds the full utterance.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("world", _make_context())
        msg1 = queue.get_nowait()
        msg2 = queue.get_nowait()
        assert msg1["correlation_token"] == msg2["correlation_token"]

    def test_aggregated_cache_holds_combined_utterance(self):
        # The cache entry for the shared token must hold the full
        # utterance joined by single spaces, so the retry path's
        # ClipboardOnlyStrategy can run TextPerfector on the whole
        # string and produce 'Hello world' (capitalized sentence start)
        # instead of pasting only the last word.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("world", _make_context())
        msg = queue.get_nowait()
        token = msg["correlation_token"]
        assert cache.get(token) == "hello world"

    def test_three_word_aggregation_preserves_order(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        for word in ("alpha", "beta", "gamma"):
            strategy.set_pending_verdict(_make_verdict())
            strategy.insert(word, _make_context())
        # Drain the queue; the last message's token names the cache
        # entry the GUI is bound to.
        last_msg = None
        while not queue.empty():
            last_msg = queue.get_nowait()
        assert last_msg is not None
        assert cache.get(last_msg["correlation_token"]) == "alpha beta gamma"

    def test_aggregation_upgrades_zero_hwnd_when_later_fragment_resolves(
        self,
    ):
        # wh-override-multiword-retry.2.1 (deepseek finding): if the
        # first fragment's HWND lookup failed (stale COM, no top-level)
        # the cache stored target_hwnd=0. A later fragment whose lookup
        # succeeds carries strictly better information; the retry
        # handler would otherwise paste into whatever holds foreground
        # at click time (the toast button). The append path upgrades
        # 0 to a non-zero HWND when a later fragment resolves one.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        # First fragment: top-level lookup raises (simulates stale COM).
        ctrl_bad = MagicMock()
        ctrl_bad.ControlTypeName = "Pane"
        ctrl_bad.GetTopLevelControl.side_effect = RuntimeError("stale com")
        context_bad = UIContext(
            focused_control=ctrl_bad,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=4242,
        )
        # Second fragment: top-level lookup succeeds.
        ctrl_good = MagicMock()
        ctrl_good.ControlTypeName = "Pane"
        top_good = MagicMock()
        top_good.NativeWindowHandle = 0xCAFE
        ctrl_good.GetTopLevelControl.return_value = top_good
        context_good = UIContext(
            focused_control=ctrl_good,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=4242,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", context_bad)
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("world", context_good)
        msg = queue.get_nowait()
        _ = queue.get_nowait()
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.target_hwnd == 0xCAFE
        assert result.target_process_id == 4242

    def test_aggregation_does_not_overwrite_nonzero_hwnd(self):
        # The upgrade only applies when the cached HWND is 0. A
        # non-zero cached HWND must not be replaced by a later
        # fragment's HWND (the "don't poison a valid HWND with
        # stale-COM" invariant the local review accepted).
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        ctrl_first = MagicMock()
        ctrl_first.ControlTypeName = "Pane"
        top_first = MagicMock()
        top_first.NativeWindowHandle = 0xAAAA
        ctrl_first.GetTopLevelControl.return_value = top_first
        context_first = UIContext(
            focused_control=ctrl_first,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=4242,
        )
        ctrl_second = MagicMock()
        ctrl_second.ControlTypeName = "Pane"
        top_second = MagicMock()
        top_second.NativeWindowHandle = 0xBBBB
        ctrl_second.GetTopLevelControl.return_value = top_second
        context_second = UIContext(
            focused_control=ctrl_second,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=9999,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", context_first)
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("world", context_second)
        msg = queue.get_nowait()
        _ = queue.get_nowait()
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.target_hwnd == 0xAAAA
        assert result.target_process_id == 4242

    def test_forget_token_removes_bucket_entry(self):
        # wh-override-multiword-retry.2.2 (deepseek finding):
        # forget_token must remove any aggregation bucket whose token
        # matches, so the retry handler can call it right after
        # invalidating the cache entry on a verified retry.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        msg = queue.get_nowait()
        token = msg["correlation_token"]
        # Bucket should hold the token.
        assert token in strategy._aggregation_buckets.values()
        strategy.forget_token(token)
        assert token not in strategy._aggregation_buckets.values()

    def test_forget_unknown_token_is_idempotent(self):
        strategy = RejectedInsertionStrategy()
        # No exception on an unknown token, no aggregation map to mutate.
        strategy.forget_token("never-stored")

    def test_forget_token_leaves_other_buckets_alone(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        verdict2 = TextTargetVerdict(
            verdict=False,
            reason="default_reject_paste_capable_class",
            supported_patterns=("Invoke",),
            control_type="Pane",
            class_name="OtherEditor::Workspace",
            process_name="zed.exe",
        )
        strategy.set_pending_verdict(verdict2)
        strategy.insert("world", _make_context())
        msg1 = queue.get_nowait()
        msg2 = queue.get_nowait()
        strategy.forget_token(msg1["correlation_token"])
        assert msg1["correlation_token"] not in strategy._aggregation_buckets.values()
        assert msg2["correlation_token"] in strategy._aggregation_buckets.values()

    def test_aggregation_preserves_hwnd_and_pid_from_first_rejection(self):
        # The user dictates the entire utterance against one target.
        # The first word's HWND/PID is the source of truth; subsequent
        # words' lookups can fail (UIA stale COM, transient focus race)
        # without invalidating the active aggregation.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )

        ctrl1 = MagicMock()
        ctrl1.ControlTypeName = "Pane"
        top1 = MagicMock()
        top1.NativeWindowHandle = 0xAAAA
        ctrl1.GetTopLevelControl.return_value = top1
        context1 = UIContext(
            focused_control=ctrl1,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=4242,
        )
        # A second context that would resolve to a different HWND; the
        # aggregator must NOT overwrite the first rejection's HWND/PID.
        ctrl2 = MagicMock()
        ctrl2.ControlTypeName = "Pane"
        top2 = MagicMock()
        top2.NativeWindowHandle = 0xBBBB
        ctrl2.GetTopLevelControl.return_value = top2
        context2 = UIContext(
            focused_control=ctrl2,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=9999,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", context1)
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("world", context2)
        msg = queue.get_nowait()
        _ = queue.get_nowait()
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.target_hwnd == 0xAAAA
        assert result.target_process_id == 4242

    def test_distinct_keys_do_not_aggregate(self):
        # Two rejections against different class_names (or any other
        # key component) live in independent aggregation buckets. The
        # cache holds one entry per token, each carrying its own word.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        verdict2 = TextTargetVerdict(
            verdict=False,
            reason="default_reject_paste_capable_class",
            supported_patterns=("Invoke",),
            control_type="Pane",
            class_name="OtherEditor::Workspace",
            process_name="zed.exe",
        )
        strategy.set_pending_verdict(verdict2)
        strategy.insert("world", _make_context())
        msg1 = queue.get_nowait()
        msg2 = queue.get_nowait()
        assert msg1["correlation_token"] != msg2["correlation_token"]
        assert cache.get(msg1["correlation_token"]) == "hello"
        assert cache.get(msg2["correlation_token"]) == "world"

    def test_aggregation_resets_after_cache_ttl_expires(self):
        # If the previous cache entry has expired, the strategy must
        # not try to append onto a dead token. It allocates a fresh
        # token so the new utterance starts with its own cache entry.
        clock = [1000.0]

        def time_source() -> float:
            return clock[0]

        queue: Queue = Queue()
        cache = RejectionTextCache(
            ttl_seconds=10.0, time_source=time_source,
        )
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        # Advance past the cache TTL so the first entry expires.
        clock[0] += 30.0
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("world", _make_context())
        msg1 = queue.get_nowait()
        msg2 = queue.get_nowait()
        assert msg1["correlation_token"] != msg2["correlation_token"]
        # First entry is gone; second entry holds only the second word.
        assert cache.get(msg2["correlation_token"]) == "world"

    def test_max_entries_eviction_does_not_strand_buckets(self):
        # wh-override-multiword-retry adversarial review finding 6:
        # the aggregation bucket map references cache tokens, but the
        # cache can evict an entry under max_entries pressure (MISS,
        # not EXPIRED). The strategy must still fall through to fresh-
        # token allocation when the bucket's token resolves as MISS.
        queue: Queue = Queue()
        cache = RejectionTextCache(max_entries=2)
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        # Three distinct keys: A, B, C. The first emission for A puts
        # an entry; the second for B puts another; the third for C
        # evicts A's entry (oldest). Then a fresh emission for A must
        # allocate a new token (cache MISS for the stranded bucket).
        a_verdict = TextTargetVerdict(
            verdict=False,
            reason="default_reject_paste_capable_class",
            supported_patterns=("Invoke",),
            control_type="Pane",
            class_name="AAA",
            process_name="zed.exe",
        )
        b_verdict = TextTargetVerdict(
            verdict=False,
            reason="default_reject_paste_capable_class",
            supported_patterns=("Invoke",),
            control_type="Pane",
            class_name="BBB",
            process_name="zed.exe",
        )
        c_verdict = TextTargetVerdict(
            verdict=False,
            reason="default_reject_paste_capable_class",
            supported_patterns=("Invoke",),
            control_type="Pane",
            class_name="CCC",
            process_name="zed.exe",
        )
        for verdict in (a_verdict, b_verdict, c_verdict):
            strategy.set_pending_verdict(verdict)
            strategy.insert("word", _make_context())

        from ui.rejection_text_cache import CacheStatus
        msg_a1 = queue.get_nowait()
        _ = queue.get_nowait()  # B
        _ = queue.get_nowait()  # C
        # A was the oldest -- evicted by max_entries=2 when C arrived.
        assert cache.resolve(msg_a1["correlation_token"]).status is CacheStatus.MISS

        # Now emit for A again. The stranded bucket should be ignored
        # and a fresh token allocated.
        strategy.set_pending_verdict(a_verdict)
        strategy.insert("again", _make_context())
        msg_a2 = queue.get_nowait()
        assert msg_a2["correlation_token"] != msg_a1["correlation_token"]
        assert cache.get(msg_a2["correlation_token"]) == "again"

    def test_post_invalidate_starts_fresh_aggregation(self):
        # wh-override-multiword-retry adversarial review finding 1: a
        # verified Try-it-anyway click invalidates the cache entry via
        # ``RejectionTextCache.invalidate``. Subsequent rejections
        # against the same target must allocate a fresh token, not
        # keep appending onto the consumed entry (the consumed token
        # is short-circuited by Logic and the user's next click would
        # be silently dropped).
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("world", _make_context())
        msg1 = queue.get_nowait()
        msg2 = queue.get_nowait()
        assert msg1["correlation_token"] == msg2["correlation_token"]

        # Simulate the verified retry path invalidating the entry.
        cache.invalidate(msg1["correlation_token"])

        # Next utterance against the same target must get a fresh token.
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("again", _make_context())
        msg3 = queue.get_nowait()
        assert msg3["correlation_token"] != msg1["correlation_token"]
        assert cache.get(msg3["correlation_token"]) == "again"

    def test_aggregation_with_perfector_handles_punctuation_join(self):
        # wh-override-multiword-retry.1.1 (codex finding): the speech
        # pipeline emits punctuation as its own fragment via the
        # period/comma/question/exclamation patterns. The unconditional
        # space join used before this finding produced "hello . world",
        # which the retry path then pasted with bad spacing. With
        # TextPerfector wired, the aggregation path composes using the
        # same spacing rules the regular paste path uses.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
            text_perfector=TextPerfector(),
        )
        for fragment in ("hello", ".", "world"):
            strategy.set_pending_verdict(_make_verdict())
            strategy.insert(fragment, _make_context())
        last_msg = None
        while not queue.empty():
            last_msg = queue.get_nowait()
        assert last_msg is not None
        assert cache.get(last_msg["correlation_token"]) == "hello. World"

    def test_aggregation_with_perfector_handles_comma_join(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
            text_perfector=TextPerfector(),
        )
        for fragment in ("hello", ",", "world"):
            strategy.set_pending_verdict(_make_verdict())
            strategy.insert(fragment, _make_context())
        last_msg = None
        while not queue.empty():
            last_msg = queue.get_nowait()
        assert last_msg is not None
        # Comma is not a sentence-ending punctuation mark, so the next
        # fragment must not capitalize.
        assert cache.get(last_msg["correlation_token"]) == "hello, world"

    def test_aggregation_with_perfector_two_words(self):
        # The plain two-word case must still produce the right text
        # when TextPerfector is wired. Without TextPerfector the join
        # is a literal space; with TextPerfector the first fragment
        # is stored raw and the second's leading space comes from the
        # perfector. Both paths produce "hello world".
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
            text_perfector=TextPerfector(),
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("world", _make_context())
        last_msg = None
        while not queue.empty():
            last_msg = queue.get_nowait()
        assert last_msg is not None
        assert cache.get(last_msg["correlation_token"]) == "hello world"

    def test_aggregation_perfector_exception_falls_back_to_space_join(
        self,
    ):
        # If TextPerfector raises during aggregation, the strategy
        # must not lose the fragment. Fall back to the legacy space
        # join so the cache still holds something useful for the retry
        # click.
        queue: Queue = Queue()
        cache = RejectionTextCache()

        class _BrokenPerfector:
            def perfected_string(self, *args, **kwargs):
                raise RuntimeError("perfector blew up")

        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
            text_perfector=_BrokenPerfector(),
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("world", _make_context())
        last_msg = None
        while not queue.empty():
            last_msg = queue.get_nowait()
        assert last_msg is not None
        assert cache.get(last_msg["correlation_token"]) == "hello world"

    def test_silenced_rejection_does_not_start_aggregation(self):
        # The non-uncertain rejection categories drop without emitting
        # an event (wh-1r2b3). They must also leave the aggregation
        # bucket alone -- otherwise an uncertain rejection that follows
        # would append onto a token that never reached the GUI and the
        # visible button would point at a different token (the
        # uncertain one) holding only its own word.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        # Silenced category: denylist_control_type.
        strategy.set_pending_verdict(_make_verdict(reason="denylist_control_type"))
        strategy.insert("silenced", _make_context())
        assert queue.qsize() == 0

        # Now an uncertain rejection: should allocate a fresh token
        # holding only its own word.
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", _make_context())
        msg = queue.get_nowait()
        assert cache.get(msg["correlation_token"]) == "hello"


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestCachePopulation:
    def test_cache_stores_token_to_original_text(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("the original dictation text", _make_context())
        msg = queue.get_nowait()
        token = msg["correlation_token"]
        assert cache.get(token) == "the original dictation text"

    def test_cache_stores_target_hwnd_from_focused_control(self):
        """wh-override-paste-focus-drift: the cache entry must carry the
        rejected target's top-level HWND so the retry handler can
        restore foreground before pasting. The HWND comes from the
        focused control's top-level window at rejection time, not from
        whatever has focus when the user clicks Try-it-anyway.
        """

        queue: Queue = Queue()
        cache = RejectionTextCache()

        # Build a context whose focused control has a known top-level HWND.
        ctrl = MagicMock()
        ctrl.ControlTypeName = "Pane"
        top = MagicMock()
        top.NativeWindowHandle = 0x12345
        ctrl.GetTopLevelControl.return_value = top
        context = UIContext(
            focused_control=ctrl,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="Zed::Window",
            process_id=12345,
        )

        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", context)
        msg = queue.get_nowait()
        token = msg["correlation_token"]
        result = cache.resolve(token)
        from ui.rejection_text_cache import CacheStatus
        assert result.status is CacheStatus.HIT
        assert result.target_hwnd == 0x12345

    def test_cache_stores_zero_hwnd_when_focused_control_has_no_top_level(self):
        """When the focused control's top-level lookup fails, the cache
        entry stores target_hwnd=0 and the retry handler skips refocus.
        """

        queue: Queue = Queue()
        cache = RejectionTextCache()

        ctrl = MagicMock()
        ctrl.ControlTypeName = "Pane"
        ctrl.GetTopLevelControl.side_effect = RuntimeError("stale com")
        context = UIContext(
            focused_control=ctrl,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="Zed::Window",
            process_id=12345,
        )

        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", context)
        msg = queue.get_nowait()
        token = msg["correlation_token"]
        result = cache.resolve(token)
        from ui.rejection_text_cache import CacheStatus
        assert result.status is CacheStatus.HIT
        assert result.target_hwnd == 0

    def test_cache_stores_target_process_id_from_context(self):
        """wh-override-paste-focus-drift.1.2: the strategy must also cache
        the rejected target's process_id so the retry handler can detect
        HWND reuse before refocusing.
        """

        queue: Queue = Queue()
        cache = RejectionTextCache()

        ctrl = MagicMock()
        ctrl.ControlTypeName = "Pane"
        top = MagicMock()
        top.NativeWindowHandle = 0x12345
        ctrl.GetTopLevelControl.return_value = top
        context = UIContext(
            focused_control=ctrl,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="Zed::Window",
            process_id=4242,
        )

        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("hello", context)
        msg = queue.get_nowait()
        token = msg["correlation_token"]
        result = cache.resolve(token)
        from ui.rejection_text_cache import CacheStatus
        assert result.status is CacheStatus.HIT
        assert result.target_hwnd == 0x12345
        assert result.target_process_id == 4242


# ---------------------------------------------------------------------------
# Privacy contract
# ---------------------------------------------------------------------------


class TestPrivacyContract:
    def test_emit_payload_never_contains_dictation_text(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        secret = "this should not appear on the wire"
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert(secret, _make_context())
        msg = queue.get_nowait()
        # Walk every value in the dict and assert the secret never
        # appears. This catches a regression where a future field gets
        # added that mistakenly carries the dictation text.
        for key, value in msg.items():
            assert secret not in repr(value), (
                f"dictation text leaked in field {key!r}"
            )


# ---------------------------------------------------------------------------
# Result contract preserved
# ---------------------------------------------------------------------------


class TestResultContract:
    def test_insert_still_returns_existing_schema_a_result(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        result = strategy.insert("hello", _make_context())
        assert result.success is True
        assert result.clipboard_dirty is False
        assert result.was_rejected is True
        assert result.rejected_reason == RejectedInsertionStrategy.DEFAULT_REASON


# ---------------------------------------------------------------------------
# Pending-verdict consumption
# ---------------------------------------------------------------------------


class TestPendingVerdict:
    def test_pending_verdict_is_consumed_by_insert(self):
        # After insert, the pending verdict is cleared so a subsequent
        # insert without a router-side set_pending_verdict does not emit
        # using stale verdict data.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        strategy.insert("first", _make_context())
        assert queue.qsize() == 1
        # Second insert without a fresh set_pending_verdict.
        strategy.insert("second", _make_context())
        # No second emit -- queue still has just the first message.
        assert queue.qsize() == 1

    def test_insert_without_pending_verdict_does_not_emit(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        # No set_pending_verdict call.
        strategy.insert("hello", _make_context())
        assert queue.qsize() == 0


# ---------------------------------------------------------------------------
# Queue failure tolerance
# ---------------------------------------------------------------------------


class TestQueueFailureTolerance:
    def test_queue_put_failure_does_not_break_insert(self, caplog):
        # If the response_queue is broken, the strategy must still
        # return a valid InsertionResult so the IPC demuxer works.
        broken_queue = MagicMock()
        broken_queue.put.side_effect = RuntimeError("queue broken")
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=broken_queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())
        result = strategy.insert("hello", _make_context())
        assert result.success is True
        assert result.was_rejected is True


# ---------------------------------------------------------------------------
# Silencing of non-uncertain rejection categories (wh-1r2b3)
# ---------------------------------------------------------------------------


def _make_browser_trap_context() -> UIContext:
    ctrl = MagicMock()
    ctrl.ControlTypeName = "Pane"
    return UIContext(
        focused_control=ctrl,
        is_flutter=False,
        is_terminal=False,
        process_name="brave.exe",
        class_name="",
        process_id=2222,
    )


def _make_browser_trap_verdict() -> TextTargetVerdict:
    return TextTargetVerdict(
        verdict=False,
        reason="default_reject",
        supported_patterns=(),
        control_type="Pane",
        class_name="",
        process_name="brave.exe",
    )


def _make_denylist_control_verdict() -> TextTargetVerdict:
    return TextTargetVerdict(
        verdict=False,
        reason="denylist_control_type",
        supported_patterns=(),
        control_type="Button",
        class_name="Button",
        process_name="explorer.exe",
    )


def _make_denylist_class_verdict() -> TextTargetVerdict:
    return TextTargetVerdict(
        verdict=False,
        reason="denylist_class_name",
        supported_patterns=(),
        control_type="Pane",
        class_name="SysListView32",
        process_name="explorer.exe",
    )


def _make_other_reason_verdict() -> TextTargetVerdict:
    # default_reject + non-browser + non-empty class -> category "other"
    return TextTargetVerdict(
        verdict=False,
        reason="default_reject",
        supported_patterns=(),
        control_type="Pane",
        class_name="Zed::Window",
        process_name="zed.exe",
    )


def _make_other_context() -> UIContext:
    ctrl = MagicMock()
    ctrl.ControlTypeName = "Pane"
    return UIContext(
        focused_control=ctrl,
        is_flutter=False,
        is_terminal=False,
        process_name="zed.exe",
        class_name="Zed::Window",
        process_id=12345,
    )


class TestSilencesNonUncertainCategories:
    """wh-1r2b3: drop the rejection event when the category is not uncertain.

    The user has no useful action on a rejection notice without a
    Try-it-anyway button. After wh-1r2b3, the Input process does not
    send the rejection event for browser_trap, definitely_not_text, or
    other categories. The GUI never sees the event, so no notice
    appears. Words are still dropped.
    """

    def test_browser_trap_does_not_emit(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_browser_trap_verdict())
        strategy.insert("hello", _make_browser_trap_context())
        assert queue.qsize() == 0

    def test_denylist_control_type_does_not_emit(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_denylist_control_verdict())
        strategy.insert("hello", _make_context())
        assert queue.qsize() == 0

    def test_denylist_class_name_does_not_emit(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_denylist_class_verdict())
        strategy.insert("hello", _make_context())
        assert queue.qsize() == 0

    def test_other_category_does_not_emit(self):
        # default_reject + non-browser + non-empty class -> other.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_other_reason_verdict())
        strategy.insert("hello", _make_other_context())
        assert queue.qsize() == 0

    def test_stale_com_does_not_emit(self):
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        verdict = TextTargetVerdict(
            verdict=False,
            reason="stale_com",
            supported_patterns=(),
            control_type="Pane",
            class_name="Zed::Window",
            process_name="zed.exe",
        )
        strategy.set_pending_verdict(verdict)
        strategy.insert("hello", _make_context())
        assert queue.qsize() == 0

    def test_uncertain_still_emits(self):
        # Sanity guard: the silencing is category-scoped. The uncertain
        # case (default_reject_paste_capable_class) must keep firing the
        # rejection event because the GUI needs to show the
        # Try-it-anyway button.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_verdict())  # default uncertain
        strategy.insert("hello", _make_context())
        assert queue.qsize() == 1
        msg = queue.get_nowait()
        assert msg["type"] == "text_target_rejected"

    def test_silenced_category_still_returns_valid_result(self):
        # The result contract is preserved regardless of category. The
        # IPC demuxer must continue to see a success=True result with
        # was_rejected=True so it does not log a stray failure.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_denylist_control_verdict())
        result = strategy.insert("hello", _make_context())
        assert result.success is True
        assert result.was_rejected is True
        assert result.clipboard_dirty is False
        assert result.rejected_reason == RejectedInsertionStrategy.DEFAULT_REASON

    def test_silenced_category_does_not_populate_cache(self):
        # The cache exists to support the Try-it-anyway replay. If we
        # are not going to show the Try-it-anyway button, the cache
        # entry has nothing to do and should not be created (saves
        # memory and removes a chance of a leak).
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        strategy.set_pending_verdict(_make_denylist_control_verdict())
        strategy.insert("the dictation", _make_context())
        assert len(cache.keys()) == 0

    def test_silenced_category_still_logs_debug_drop(self, caplog):
        # The DEBUG log line that records the dropped insertion is
        # preserved across the silencing change. The diagnostic stream
        # should not regress.
        import logging as logging_mod

        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        with caplog.at_level(logging_mod.DEBUG, logger="ui.strategies.specific"):
            strategy.set_pending_verdict(_make_denylist_control_verdict())
            strategy.insert("hello", _make_context())
        drop_count = sum(
            1 for r in caplog.records
            if r.levelno == logging_mod.DEBUG
            and "dropping insert" in r.message
        )
        assert drop_count == 1, (
            f"expected 1 DEBUG drop log, got {drop_count}; "
            f"records: {[(r.levelname, r.name, r.message) for r in caplog.records]}"
        )

    def test_silenced_category_still_fires_first_log_map_info(self, caplog):
        # FirstRejectionLogMap escalates the first rejection per key
        # to INFO. The silencing change suppresses the IPC event, NOT
        # the diagnostic INFO log. Operators reading the log must
        # still see one INFO line per (process, class, control_type,
        # reason) key per session.
        import logging as logging_mod
        from rejection_rate_limit import FirstRejectionLogMap

        queue: Queue = Queue()
        cache = RejectionTextCache()
        log_map = FirstRejectionLogMap()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
            first_log_map=log_map,
        )
        with caplog.at_level(logging_mod.INFO, logger="ui.strategies.specific"):
            strategy.set_pending_verdict(_make_denylist_control_verdict())
            strategy.insert("hello", _make_context())
        info_count = sum(
            1 for r in caplog.records
            if r.levelno == logging_mod.INFO
            and "first per key" in r.message
        )
        assert info_count == 1, (
            f"expected 1 INFO 'first per key' log even when silenced, "
            f"got {info_count}"
        )
