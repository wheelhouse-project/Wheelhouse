"""Tests for rejection rate-limit / dedup maps (wh-zib65).

Two independent maps live in two processes (per wh-x4mv.9 review):

  * GUI process owns ``ToastSuppressionMap``: the toast is suppressed
    if a (process, class, reason) key was shown less than 60 seconds
    ago. The map also reports whether the toast is the first or a
    repeat for that key so the GUI can pick the dwell time
    (8 seconds first, 4 seconds repeat).

  * Input process owns ``FirstRejectionLogMap``: logs INFO once per
    (process, class, reason) key so the diagnostic surface is not
    swamped by per-word logging during continuous dictation against
    the same wrong target.

The two maps do NOT coordinate. The tests assert each map's behaviour
in isolation.
"""

from __future__ import annotations

from rejection_rate_limit import (
    FirstRejectionLogMap,
    ToastSuppressionMap,
    reescalation_seconds_from_config,
)


class _Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestToastSuppressionMap:
    def test_first_show_returns_first_dwell(self):
        clock = _Clock()
        m = ToastSuppressionMap(time_source=clock)
        decision = m.decide(("zed.exe", "Zed::Window", "WindowControl", "default_reject_paste_capable_class"))
        assert decision.show is True
        assert decision.is_first is True
        assert decision.lifetime_ms == 8000

    def test_second_show_within_cooldown_is_suppressed(self):
        clock = _Clock()
        m = ToastSuppressionMap(time_source=clock)
        m.decide(("zed.exe", "Zed::Window", "WindowControl", "default_reject_paste_capable_class"))
        clock.advance(30.0)
        decision = m.decide(("zed.exe", "Zed::Window", "WindowControl", "default_reject_paste_capable_class"))
        assert decision.show is False

    def test_second_show_after_cooldown_uses_repeat_dwell(self):
        clock = _Clock()
        m = ToastSuppressionMap(time_source=clock, cooldown_seconds=60.0)
        m.decide(("zed.exe", "Zed::Window", "WindowControl", "default_reject_paste_capable_class"))
        clock.advance(60.5)
        decision = m.decide(("zed.exe", "Zed::Window", "WindowControl", "default_reject_paste_capable_class"))
        assert decision.show is True
        assert decision.is_first is False
        assert decision.lifetime_ms == 4000

    def test_different_keys_independent(self):
        clock = _Clock()
        m = ToastSuppressionMap(time_source=clock)
        a = m.decide(("zed.exe", "Zed::Window", "WindowControl", "default_reject_paste_capable_class"))
        b = m.decide(("brave.exe", "", "DocumentControl", "default_reject"))
        assert a.show and b.show
        assert a.is_first and b.is_first

    def test_repeat_after_repeat_still_repeat(self):
        clock = _Clock()
        m = ToastSuppressionMap(time_source=clock)
        key = ("zed.exe", "Zed::Window", "WindowControl", "default_reject_paste_capable_class")
        m.decide(key)
        clock.advance(61.0)
        m.decide(key)
        clock.advance(61.0)
        decision = m.decide(key)
        # Still repeat (not first) on third visible show.
        assert decision.show is True
        assert decision.is_first is False
        assert decision.lifetime_ms == 4000

    def test_custom_cooldown_and_dwell(self):
        clock = _Clock()
        m = ToastSuppressionMap(
            time_source=clock,
            cooldown_seconds=10.0,
            first_dwell_ms=5000,
            repeat_dwell_ms=2000,
        )
        d1 = m.decide(("p", "c", "ct", "r"))
        assert d1.show and d1.lifetime_ms == 5000
        clock.advance(5.0)
        assert m.decide(("p", "c", "ct", "r")).show is False
        clock.advance(6.0)
        d3 = m.decide(("p", "c", "ct", "r"))
        assert d3.show and d3.lifetime_ms == 2000


class TestFirstRejectionLogMap:
    def test_first_call_returns_true(self):
        m = FirstRejectionLogMap()
        assert m.should_log(("zed.exe", "Zed::Window", "WindowControl", "default_reject_paste_capable_class")) is True

    def test_subsequent_calls_return_false(self):
        m = FirstRejectionLogMap()
        key = ("zed.exe", "Zed::Window", "WindowControl", "default_reject_paste_capable_class")
        assert m.should_log(key) is True
        assert m.should_log(key) is False
        assert m.should_log(key) is False

    def test_different_keys_independent(self):
        m = FirstRejectionLogMap()
        a = ("zed.exe", "Zed::Window", "WindowControl", "default_reject_paste_capable_class")
        b = ("brave.exe", "", "DocumentControl", "default_reject")
        assert m.should_log(a) is True
        assert m.should_log(b) is True
        assert m.should_log(a) is False
        assert m.should_log(b) is False

    def test_does_not_persist_across_instances(self):
        # The map resets on process restart; constructing a new
        # instance gives back True for the same key.
        a = FirstRejectionLogMap()
        b = FirstRejectionLogMap()
        a.should_log(("p", "c", "ct", "r"))
        assert b.should_log(("p", "c", "ct", "r")) is True


class TestFirstRejectionLogMapReescalation:
    """wh-rejection-log-reescalation: optional INFO re-escalation window.

    Default (reescalation_seconds=0) keeps the wh-zib65 contract: one
    INFO per key, ever. A positive window lets the INFO line fire again
    once the window has elapsed, so a persistent rejection stays
    visible at INFO level instead of looking self-resolved.
    """

    KEY = ("zed.exe", "Zed::Window", "WindowControl",
           "default_reject_paste_capable_class")

    def test_disabled_by_default_never_relogs(self):
        clock = _Clock()
        m = FirstRejectionLogMap(time_source=clock)
        assert m.should_log(self.KEY) is True
        clock.advance(100_000.0)
        assert m.should_log(self.KEY) is False

    def test_relogs_after_window_elapses(self):
        clock = _Clock()
        m = FirstRejectionLogMap(
            reescalation_seconds=30.0, time_source=clock
        )
        assert m.should_log(self.KEY) is True
        clock.advance(29.0)
        assert m.should_log(self.KEY) is False
        clock.advance(1.5)
        assert m.should_log(self.KEY) is True
        # Window restarts from the re-escalated log, not the first one.
        clock.advance(29.0)
        assert m.should_log(self.KEY) is False
        clock.advance(1.5)
        assert m.should_log(self.KEY) is True

    def test_suppressed_calls_do_not_extend_the_window(self):
        clock = _Clock()
        m = FirstRejectionLogMap(
            reescalation_seconds=30.0, time_source=clock
        )
        m.should_log(self.KEY)
        for _ in range(10):
            clock.advance(2.9)
            m.should_log(self.KEY)
        clock.advance(2.0)  # 31s since the first log
        assert m.should_log(self.KEY) is True

    def test_keys_reescalate_independently(self):
        clock = _Clock()
        m = FirstRejectionLogMap(
            reescalation_seconds=30.0, time_source=clock
        )
        other = ("brave.exe", "", "DocumentControl", "default_reject")
        assert m.should_log(self.KEY) is True
        clock.advance(20.0)
        assert m.should_log(other) is True
        clock.advance(15.0)  # KEY at 35s, other at 15s
        assert m.should_log(self.KEY) is True
        assert m.should_log(other) is False


class TestReescalationSecondsFromConfig:
    def test_missing_sections_default_to_zero(self):
        assert reescalation_seconds_from_config({}) == 0.0
        assert reescalation_seconds_from_config(
            {"ui_actions": {}}
        ) == 0.0
        assert reescalation_seconds_from_config(
            {"ui_actions": {"text_target": {}}}
        ) == 0.0

    def test_reads_configured_value(self):
        cfg = {"ui_actions": {"text_target": {
            "rejection_reescalation_seconds": 45,
        }}}
        assert reescalation_seconds_from_config(cfg) == 45.0

    def test_bad_values_degrade_to_zero(self):
        for bad in ("soon", None, [], {}, -5):
            cfg = {"ui_actions": {"text_target": {
                "rejection_reescalation_seconds": bad,
            }}}
            assert reescalation_seconds_from_config(cfg) == 0.0, bad
