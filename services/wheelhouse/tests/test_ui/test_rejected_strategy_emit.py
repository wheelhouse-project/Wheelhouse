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
from unittest.mock import MagicMock, patch

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

    def test_aggregation_splits_off_complete_fragment_from_zero_identity_entry(
        self,
    ):
        # wh-ensure-focused-same-process-fallback.1.15 (codex round
        # 10): the old upgrade path re-bound the earlier fragments'
        # text to a LATER fragment's window without any proof the
        # earlier text was dictated at that window -- a focus switch
        # between same-key windows pasted fragment-1 text into the
        # fragment-2 window. An entry with no provenance tag can
        # never be proven to share a window with anything, so a
        # complete later fragment starts its OWN entry (replayable
        # alone) and the incomplete entry stays behind, unreplayable.
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
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0xCAFE,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=5,
        ):
            strategy.insert("world", context_good)
        msg1 = queue.get_nowait()
        msg2 = queue.get_nowait()
        assert msg1["correlation_token"] != msg2["correlation_token"]
        from ui.rejection_text_cache import CacheStatus
        result1 = cache.resolve(msg1["correlation_token"])
        assert result1.status is CacheStatus.HIT
        assert result1.text == "hello"
        assert result1.target_hwnd == 0
        result2 = cache.resolve(msg2["correlation_token"])
        assert result2.status is CacheStatus.HIT
        assert result2.text == "world"
        assert result2.target_hwnd == 0xCAFE
        assert result2.target_root == 0xCAFE
        assert result2.target_tag == 5

    def test_identity_capture_refuses_marker_written_after_recycle(self):
        # wh-ensure-focused-same-process-fallback.1.17 (codex round
        # 11): _resolve_target_identity sampled the root BEFORE
        # writing the provenance marker. Windows can destroy the
        # target after GetAncestor returns its own-root handle and
        # recycle the numeric handle as a new same-PID own-root
        # top-level before SetProp runs -- the marker then lands on
        # the RECYCLED window and the cache stores an identity every
        # retry-time probe confirms against the wrong window. The
        # resolve must acquire the marker first and re-acquire it
        # after the root sample; tag_hwnd_provenance returns the
        # existing marker for a live object and a NEW unique marker
        # for a recycled one, so inequality proves the object died
        # mid-resolve and the entry must store tag 0 (unreplayable).
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        ctrl = MagicMock()
        ctrl.ControlTypeName = "Pane"
        top = MagicMock()
        top.NativeWindowHandle = 0xAAAA
        ctrl.GetTopLevelControl.return_value = top
        context = UIContext(
            focused_control=ctrl,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=4242,
        )
        # Model the window OBJECT separately from the numeric handle:
        # markers live on the object and die with it. The root-sample
        # call destroys the object and recycles the handle, exactly
        # the interleave codex described.
        live_window = ["A"]
        markers: dict = {}
        next_marker = [7]

        def fake_tag(hwnd):
            win = live_window[0]
            if win not in markers:
                markers[win] = next_marker[0]
                next_marker[0] += 1
            return markers[win]

        def fake_normalize(hwnd):
            # GetAncestor answers for the live object; then the
            # object dies and the handle is reborn as own-root B.
            live_window[0] = "B"
            return 0xAAAA

        strategy.set_pending_verdict(_make_verdict())
        with patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            side_effect=fake_tag,
        ), patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            side_effect=fake_normalize,
        ):
            strategy.insert("hello", context)
        msg = queue.get_nowait()
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.text == "hello"
        assert result.target_tag == 0

    def test_identity_capture_refuses_recycle_before_first_tag(self):
        # wh-ensure-focused-same-process-fallback.1.26 (codex round
        # 17): the .1.17 recheck only covers a recycle AFTER the
        # first tag. If the target dies and its handle is recycled as
        # a same-PID own-root window BETWEEN the NativeWindowHandle
        # read and the FIRST tag_hwnd_provenance call, the first tag
        # lands on the recycled window, the root sample and the .1.17
        # re-tag both see the recycled window consistently, and every
        # retry-time probe then proves the wrong window. The resolve
        # must re-derive the top-level from the control AFTER tagging
        # and store tag 0 unless the control still resolves to the
        # tagged handle -- a stale control (its window died) raises,
        # which must read as refusal.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        ctrl = MagicMock()
        ctrl.ControlTypeName = "Pane"
        top = MagicMock()
        top.NativeWindowHandle = 0xAAAA
        live_window = ["A"]

        def get_top():
            # wh-ensure-focused-same-process-fallback.1.29 (codex
            # round 18): the control answers for whichever window
            # OBJECT is currently alive, not for a call count. While
            # window A lives, the lookup resolves normally; once the
            # recycle happens (fake_tag flips live_window), the
            # control's element is stale and the lookup raises. A
            # call-count fixture answered the same way no matter WHEN
            # the re-derive ran, so a regression that moved the
            # re-derive BEFORE tagging -- where it still sees live
            # window A and blesses the tag -- passed the old test.
            if live_window[0] == "A":
                return top
            raise RuntimeError("UIA element not available")

        ctrl.GetTopLevelControl.side_effect = get_top
        context = UIContext(
            focused_control=ctrl,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=4242,
        )
        markers: dict = {}
        next_marker = [7]

        def fake_tag(hwnd):
            # The recycle lands BEFORE the first SetProp: window A
            # dies as the first tag call runs, so the marker is
            # written onto recycled window B and stays stable for the
            # .1.17 re-tag. Tying the recycle to the tag call is what
            # makes this fixture prove ORDER (.1.29): only a
            # re-derive that runs AFTER this flip observes the stale
            # control and refuses.
            live_window[0] = "B"
            win = live_window[0]
            if win not in markers:
                markers[win] = next_marker[0]
                next_marker[0] += 1
            return markers[win]

        strategy.set_pending_verdict(_make_verdict())
        with patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            side_effect=fake_tag,
        ), patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0xAAAA,
        ):
            strategy.insert("hello", context)
        msg = queue.get_nowait()
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.text == "hello"
        assert result.target_tag == 0

    def test_identity_capture_refuses_when_control_rebinds_elsewhere(self):
        # wh-ensure-focused-same-process-fallback.1.26 companion: the
        # post-tag re-derive can also come back with a DIFFERENT
        # top-level handle (the control's element re-resolved into
        # another window's tree). Any answer other than the tagged
        # handle must store tag 0.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        ctrl = MagicMock()
        ctrl.ControlTypeName = "Pane"
        top_a = MagicMock()
        top_a.NativeWindowHandle = 0xAAAA
        top_b = MagicMock()
        top_b.NativeWindowHandle = 0xBBBB
        calls = {"n": 0}

        def get_top():
            calls["n"] += 1
            return top_a if calls["n"] == 1 else top_b

        ctrl.GetTopLevelControl.side_effect = get_top
        context = UIContext(
            focused_control=ctrl,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=4242,
        )
        strategy.set_pending_verdict(_make_verdict())
        with patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=9,
        ), patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0xAAAA,
        ):
            strategy.insert("hello", context)
        msg = queue.get_nowait()
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.text == "hello"
        assert result.target_tag == 0

    def test_aggregation_splits_when_fragments_prove_different_windows(self):
        # wh-ensure-focused-same-process-fallback.1.15 (codex round
        # 10): the aggregation key is (process, class, control_type,
        # reason) -- two windows of the same app share it. Before this
        # fix, a focus switch between same-key windows A and B
        # appended B's text onto A's entry, and the retry pasted B's
        # words into A. The provenance markers prove the fragments
        # came from different window objects, so the strategy must
        # start a separate entry for the B fragment.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        ctrl_a = MagicMock()
        ctrl_a.ControlTypeName = "Pane"
        top_a = MagicMock()
        top_a.NativeWindowHandle = 0xAAAA
        ctrl_a.GetTopLevelControl.return_value = top_a
        context_a = UIContext(
            focused_control=ctrl_a,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=4242,
        )
        ctrl_b = MagicMock()
        ctrl_b.ControlTypeName = "Pane"
        top_b = MagicMock()
        top_b.NativeWindowHandle = 0xBBBB
        ctrl_b.GetTopLevelControl.return_value = top_b
        context_b = UIContext(
            focused_control=ctrl_b,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=4242,
        )
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            side_effect=lambda h: h,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            side_effect=lambda h: {0xAAAA: 5, 0xBBBB: 9}[h],
        ):
            strategy.set_pending_verdict(_make_verdict())
            strategy.insert("hello", context_a)
            strategy.set_pending_verdict(_make_verdict())
            strategy.insert("world", context_b)
        msg1 = queue.get_nowait()
        msg2 = queue.get_nowait()
        assert msg1["correlation_token"] != msg2["correlation_token"]
        from ui.rejection_text_cache import CacheStatus
        result1 = cache.resolve(msg1["correlation_token"])
        assert result1.status is CacheStatus.HIT
        assert result1.text == "hello"
        assert result1.target_hwnd == 0xAAAA
        assert result1.target_tag == 5
        result2 = cache.resolve(msg2["correlation_token"])
        assert result2.status is CacheStatus.HIT
        assert result2.text == "world"
        assert result2.target_hwnd == 0xBBBB
        assert result2.target_root == 0xBBBB
        assert result2.target_tag == 9

    def test_aggregation_splits_when_fragment_identity_unresolved(self):
        # wh-ensure-focused-same-process-fallback.1.15: a fragment
        # whose provenance tagging fails cannot be proven to share a
        # window with the complete cached entry -- even when its
        # HANDLE VALUE matches, because a recycled handle carries the
        # same value while naming a different window object. The
        # complete entry stays clean and replayable; the unproven
        # fragment starts its own (unreplayable) entry.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        ctrl = MagicMock()
        ctrl.ControlTypeName = "Pane"
        top = MagicMock()
        top.NativeWindowHandle = 0xAAAA
        ctrl.GetTopLevelControl.return_value = top
        context = UIContext(
            focused_control=ctrl,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=4242,
        )
        strategy.set_pending_verdict(_make_verdict())
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0xAAAA,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=5,
        ):
            strategy.insert("hello", context)
        strategy.set_pending_verdict(_make_verdict())
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0xAAAA,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=0,
        ):
            strategy.insert("world", context)
        msg1 = queue.get_nowait()
        msg2 = queue.get_nowait()
        assert msg1["correlation_token"] != msg2["correlation_token"]
        from ui.rejection_text_cache import CacheStatus
        result1 = cache.resolve(msg1["correlation_token"])
        assert result1.status is CacheStatus.HIT
        assert result1.text == "hello"
        assert result1.target_tag == 5
        result2 = cache.resolve(msg2["correlation_token"])
        assert result2.status is CacheStatus.HIT
        assert result2.text == "world"
        assert result2.target_tag == 0

    def test_aggregation_keeps_first_root_when_same_window_proven(self):
        # First-fragment-is-source-of-truth survives the .1.15 gate:
        # when the markers prove the same window, the entry keeps the
        # first fragment's root snapshot even if a later fragment's
        # normalization returns a different root (reparenting during
        # the utterance). The retry handler's live-root comparison is
        # the layer that decides what to do about the drift.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        ctrl = MagicMock()
        ctrl.ControlTypeName = "Pane"
        top = MagicMock()
        top.NativeWindowHandle = 0xAAAA
        ctrl.GetTopLevelControl.return_value = top
        context = UIContext(
            focused_control=ctrl,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=4242,
        )
        strategy.set_pending_verdict(_make_verdict())
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0xAAAA,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=5,
        ):
            strategy.insert("hello", context)
        strategy.set_pending_verdict(_make_verdict())
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0xBBBB,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=5,
        ):
            strategy.insert("world", context)
        msg1 = queue.get_nowait()
        msg2 = queue.get_nowait()
        assert msg1["correlation_token"] == msg2["correlation_token"]
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg1["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.text == "hello world"
        assert result.target_root == 0xAAAA

    def test_aggregation_merges_unresolvable_fragments(self):
        # wh-ensure-focused-same-process-fallback.1.15, deliberate
        # design: when NEITHER side carries a provenance marker, the
        # fragments merge under one token. The merged entry can never
        # paste (the retry handler refuses tag=0 outright), so no
        # wrong-window replay is possible; merging keeps the GUI's
        # one-toast-one-token behaviour for the common transient
        # failure. This is NOT a 0 == 0 identity credit -- nothing
        # downstream treats the merged entry as proven.
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        ctrl = MagicMock()
        ctrl.ControlTypeName = "Pane"
        top = MagicMock()
        top.NativeWindowHandle = 0xAAAA
        ctrl.GetTopLevelControl.return_value = top
        context = UIContext(
            focused_control=ctrl,
            is_flutter=False,
            is_terminal=False,
            process_name="zed.exe",
            class_name="zed::Workspace",
            process_id=4242,
        )
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0xAAAA,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=0,
        ):
            strategy.set_pending_verdict(_make_verdict())
            strategy.insert("hello", context)
            strategy.set_pending_verdict(_make_verdict())
            strategy.insert("world", context)
        msg1 = queue.get_nowait()
        msg2 = queue.get_nowait()
        assert msg1["correlation_token"] == msg2["correlation_token"]
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg1["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.text == "hello world"
        assert result.target_tag == 0

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
        entry stores target_hwnd=0. The retry handler refuses such an
        entry outright with token_expired (.1.11) -- 0 is a data-shape
        default, not replay permission.
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

    def test_cache_stores_target_root_snapshot(self):
        """wh-ensure-focused-same-process-fallback.1.7: the strategy must
        also cache the GA_ROOT normalization of the target HWND taken
        at rejection time. The retry handler compares the live root
        against this snapshot to detect a same-process handle recycle,
        which the PID guard alone cannot see. The stored value is the
        NORMALIZED root, not the raw HWND -- the two differ when UIA's
        top-level is a child of the actual Win32 root.
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
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
        ) as mock_normalize:
            mock_normalize.return_value = 0x99999
            strategy.insert("hello", context)
        mock_normalize.assert_called_once_with(0x12345)
        msg = queue.get_nowait()
        token = msg["correlation_token"]
        result = cache.resolve(token)
        from ui.rejection_text_cache import CacheStatus
        assert result.status is CacheStatus.HIT
        assert result.target_hwnd == 0x12345
        assert result.target_root == 0x99999

    def test_cache_stores_provenance_tag(self):
        """wh-ensure-focused-same-process-fallback.1.12: the strategy
        tags the target window OBJECT at rejection time (SetProp) and
        caches the marker. The retry handler re-reads the property
        from the live window and refuses on mismatch -- the one probe
        a SAME-RUN numeric handle recycle cannot alias (cross-run,
        equal 43-bit salts collide at about 2**-43 per pair of runs
        -- the accepted residual at _RUN_SALT), because the property
        dies with the window object.
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
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0x12345,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=7,
        ) as mock_tag:
            strategy.insert("hello", context)
        # .1.17: the resolve acquires the marker BEFORE the root
        # sample and re-acquires it after -- two calls, same handle.
        # Equal results across the pair prove the window object
        # survived the resolve, so the tag is stored.
        assert mock_tag.call_count == 2
        for tag_call in mock_tag.call_args_list:
            assert tag_call.args == (0x12345,)
        msg = queue.get_nowait()
        result = cache.resolve(msg["correlation_token"])
        from ui.rejection_text_cache import CacheStatus
        assert result.status is CacheStatus.HIT
        assert result.target_tag == 7

    def test_cache_stores_zero_root_when_normalization_fails(self):
        """When the rejection-time GA_ROOT normalization fails, the entry
        stores target_root=0. The retry handler returns token_expired
        for such an entry (.1.8/.1.11) -- a replayable entry needs the
        complete hwnd/root/tag identity.
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
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
        ) as mock_normalize:
            mock_normalize.return_value = None
            strategy.insert("hello", context)
        msg = queue.get_nowait()
        token = msg["correlation_token"]
        result = cache.resolve(token)
        from ui.rejection_text_cache import CacheStatus
        assert result.status is CacheStatus.HIT
        assert result.target_hwnd == 0x12345
        assert result.target_root == 0

    def test_aggregation_split_entry_carries_full_identity(self):
        """wh-ensure-focused-same-process-fallback.1.15: when a
        complete fragment splits away from an unprovable cached entry,
        the FRESH entry must carry the fragment's full identity --
        hwnd, pid, root snapshot, and provenance tag -- so its own
        retry passes every gate. A fresh entry missing any of them
        would be refused at retry and the split would silently lose
        the replayable half.
        """

        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
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
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0xCAFE,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=5,
        ):
            strategy.insert("world", context_good)
        _ = queue.get_nowait()
        msg2 = queue.get_nowait()
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg2["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.text == "world"
        assert result.target_hwnd == 0xCAFE
        assert result.target_process_id == 4242
        assert result.target_root == 0xCAFE
        assert result.target_tag == 5

    def test_aggregation_upgrades_rootless_entry_from_later_fragment(self):
        """wh-ensure-focused-same-process-fallback.1.13: a first fragment
        can resolve a nonzero HWND while the GA_ROOT normalization
        transiently fails, caching (hwnd, pid, root=0) -- an entry the
        .1.8 retry gate refuses outright. The append path only
        re-resolved when the cached HWND was 0, so a second fragment
        with a complete identity re-wrote root=0 and left the whole
        aggregated dictation unrecoverable. The upgrade must also run
        for a rootless cached identity, and take the later fragment's
        complete (hwnd, root) pair.
        """

        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
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
            class_name="zed::Workspace",
            process_id=4242,
        )
        strategy.set_pending_verdict(_make_verdict())
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=None,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=5,
        ):
            # Fragment 1: HWND resolves, root normalization fails.
            strategy.insert("hello", context)
        strategy.set_pending_verdict(_make_verdict())
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0x12345,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=5,
        ):
            # Fragment 2: complete identity.
            strategy.insert("world", context)
        msg = queue.get_nowait()
        _ = queue.get_nowait()
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.target_hwnd == 0x12345
        assert result.target_root == 0x12345

    def test_aggregation_keeps_rootless_entry_when_no_fragment_completes(self):
        """The rootless upgrade must stay refuse-by-default: when every
        fragment's normalization fails, the entry keeps root=0 (the
        .1.8 gate then refuses the retry). The upgrade never invents a
        root and never takes a partial identity."""

        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
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
            class_name="zed::Workspace",
            process_id=4242,
        )
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
        ) as mock_normalize:
            mock_normalize.return_value = None
            strategy.set_pending_verdict(_make_verdict())
            strategy.insert("hello", context)
            strategy.set_pending_verdict(_make_verdict())
            strategy.insert("world", context)
        msg = queue.get_nowait()
        _ = queue.get_nowait()
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.target_hwnd == 0x12345
        assert result.target_root == 0

    def test_aggregation_splits_off_tagged_fragment_from_tagless_entry(self):
        """wh-ensure-focused-same-process-fallback.1.15: a first
        fragment can resolve hwnd and root while SetProp transiently
        fails, caching tag=0 -- an entry that can never be proven to
        share a window with anything, even a fragment carrying the
        SAME handle value (a recycled handle keeps the value while
        naming a new window object). A later complete fragment must
        NOT be appended or have its identity grafted onto that entry
        (the pre-.1.15 upgrade did exactly that and re-bound the
        earlier text to an unproven window); it starts its own
        replayable entry, and the tagless entry stays behind,
        refused at retry."""

        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
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
            class_name="zed::Workspace",
            process_id=4242,
        )
        strategy.set_pending_verdict(_make_verdict())
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0x12345,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=0,
        ):
            # Fragment 1: hwnd and root resolve, tagging fails.
            strategy.insert("hello", context)
        strategy.set_pending_verdict(_make_verdict())
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0x12345,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=9,
        ):
            # Fragment 2: complete identity.
            strategy.insert("world", context)
        msg1 = queue.get_nowait()
        msg2 = queue.get_nowait()
        assert msg1["correlation_token"] != msg2["correlation_token"]
        from ui.rejection_text_cache import CacheStatus
        result1 = cache.resolve(msg1["correlation_token"])
        assert result1.status is CacheStatus.HIT
        assert result1.text == "hello"
        assert result1.target_tag == 0
        result2 = cache.resolve(msg2["correlation_token"])
        assert result2.status is CacheStatus.HIT
        assert result2.text == "world"
        assert result2.target_hwnd == 0x12345
        assert result2.target_root == 0x12345
        assert result2.target_tag == 9

    def test_aggregation_zero_hwnd_not_upgraded_by_untagged_identity(self):
        """wh-ensure-focused-same-process-fallback.1.12: the upgrade
        takes only a COMPLETE identity, and the provenance tag is part
        of it. A later fragment that resolves hwnd and root but whose
        SetProp fails must not half-upgrade a no-identity entry."""

        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
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
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=0xCAFE,
        ), patch(
            "ui.strategies.specific.tag_hwnd_provenance",
            return_value=0,
        ):
            strategy.insert("world", context_good)
        msg = queue.get_nowait()
        _ = queue.get_nowait()
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.target_hwnd == 0
        assert result.target_tag == 0

    def test_aggregation_zero_hwnd_not_upgraded_by_partial_identity(self):
        """wh-ensure-focused-same-process-fallback.1.13: the upgrade
        takes only a COMPLETE (nonzero hwnd AND root) replacement. A
        later fragment that resolves an HWND but whose root
        normalization fails must not half-upgrade a no-identity entry:
        the partial (hwnd, root=0) shape is refused by the retry
        handler exactly like hwnd=0, and pairing the text with a
        half-verified window invites misdirected pastes if the gates
        ever relax."""

        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
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
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            return_value=None,
        ):
            strategy.insert("world", context_good)
        msg = queue.get_nowait()
        _ = queue.get_nowait()
        from ui.rejection_text_cache import CacheStatus
        result = cache.resolve(msg["correlation_token"])
        assert result.status is CacheStatus.HIT
        assert result.target_hwnd == 0
        assert result.target_root == 0


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

    def test_elevated_emits_notice_event(self):
        # wh-elevated-target-notice: the elevated category is the
        # second category (after uncertain) that sends the rejection
        # event. The GUI shows the administrator notice WITHOUT the
        # Try-it-anyway button (button visibility stays gated on
        # should_show_try_anyway, which is False for elevated).
        queue: Queue = Queue()
        cache = RejectionTextCache()
        strategy = RejectedInsertionStrategy(
            response_queue=queue, text_cache=cache,
        )
        verdict = TextTargetVerdict(
            verdict=False,
            reason="elevated_process_window",
            supported_patterns=(),
            control_type="",
            class_name="RegEdit_RegEdit",
            process_name="regedit.exe",
        )
        strategy.set_pending_verdict(verdict)
        strategy.insert("hello", _make_context())
        assert queue.qsize() == 1
        msg = queue.get_nowait()
        assert msg["type"] == "text_target_rejected"
        assert msg["reason"] == "elevated_process_window"

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
