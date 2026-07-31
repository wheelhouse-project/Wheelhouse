"""Tests for the sounddevice capture path's starvation defenses.

wh-sounddevice-starvation-parity: the WinRT capture thread elevates itself to
time-critical priority and the google consumer loop tracks scheduling stalls,
but the sounddevice path (MicrophoneStream) had neither -- and it is the path
the distil and parakeet providers always use.

Measured on the reference machine (2026-07-28, review finding
wh-sounddevice-starvation-parity.3.1): PortAudio's MME host API already runs
the callback thread at TIME_CRITICAL before any of our code executes. So the
defense is conditional: measure the callback thread's priority once per stream
start, elevate only if it is below time-critical, and report the measured
value. The callback itself must stay free of logging and of per-frame lock
acquisitions beyond the queue put (review finding
wh-sounddevice-starvation-parity.3.2): log lines are handed to read() on the
consumer thread, and the stall tracker reads queue depth lazily, only when a
rate-limited stall message actually forms.
"""
import numpy as np
import pytest
from unittest.mock import Mock, patch

from shared_audio.diagnostics import LoopStallTracker
from shared_audio.microphone import MicrophoneStream


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _invoke_callback(mic, status=None):
    """Invoke the PortAudio callback with a 30ms frame of silence."""
    indata = np.zeros((480, 1), dtype=np.int16)
    mic._callback(indata, 480, None, status)


class TestConditionalCallbackElevation:
    def test_already_time_critical_thread_is_not_touched(self):
        """MME (the default host API) pre-elevates the callback thread to
        TIME_CRITICAL; SetThreadPriority must not run on top of that."""
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=15):
            with patch('shared_audio.microphone.elevate_current_thread',
                       return_value=True) as elevate:
                mic = MicrophoneStream()
                _invoke_callback(mic)
                elevate.assert_not_called()

    def test_below_time_critical_thread_is_elevated(self):
        """A host API that leaves the callback thread at normal priority is
        the case the defense exists for."""
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=0):
            with patch('shared_audio.microphone.elevate_current_thread',
                       return_value=True) as elevate:
                mic = MicrophoneStream()
                _invoke_callback(mic)
                elevate.assert_called_once_with('time_critical')

    def test_unreadable_priority_skips_elevation(self):
        """If the priority cannot be read (None), do not blind-fire
        SetThreadPriority over unknown host scheduling."""
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=None):
            with patch('shared_audio.microphone.elevate_current_thread',
                       return_value=True) as elevate:
                mic = MicrophoneStream()
                _invoke_callback(mic)
                elevate.assert_not_called()

    def test_unreadable_priority_reports_unavailable(self, caplog):
        """An unreadable priority must not be reported as host-managed
        (wh-sounddevice-starvation-parity.3.4): the probe learned nothing,
        and the log line must say so."""
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=None):
            with patch('shared_audio.microphone.elevate_current_thread',
                       return_value=True):
                mic = MicrophoneStream()
                _invoke_callback(mic)
                with caplog.at_level("INFO", logger="shared_audio.microphone"):
                    mic.read(timeout=0.05)
        prio_lines = [r for r in caplog.records
                      if "Callback thread priority" in r.message]
        assert len(prio_lines) == 1
        assert prio_lines[0].message == (
            "[mic] Callback thread priority unavailable; elevation skipped")

    def test_priority_checked_only_once_across_callbacks(self):
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=15) as get_prio:
            with patch('shared_audio.microphone.elevate_current_thread',
                       return_value=True):
                mic = MicrophoneStream()
                for _ in range(5):
                    _invoke_callback(mic)
                assert get_prio.call_count == 1

    def test_start_re_arms_check_for_new_portaudio_thread(self):
        """stop()/start() makes PortAudio create a fresh callback thread;
        the priority check must run again on it."""
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=0) as get_prio:
            with patch('shared_audio.microphone.elevate_current_thread',
                       return_value=True) as elevate:
                with patch('shared_audio.microphone.sd.InputStream') as MockStream:
                    MockStream.return_value = Mock(active=False)
                    mic = MicrophoneStream()
                    _invoke_callback(mic)
                    mic.stop()
                    mic.start()
                    _invoke_callback(mic)
                assert get_prio.call_count == 2
                assert elevate.call_count == 2

    def test_elevation_failure_does_not_break_capture(self):
        """elevate_current_thread returning False (no ctypes, denied) must not
        prevent audio from being queued."""
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=0):
            with patch('shared_audio.microphone.elevate_current_thread',
                       return_value=False):
                mic = MicrophoneStream()
                _invoke_callback(mic)
                assert mic.get_queue_size() == 1

    def test_callback_emits_no_log_records(self, caplog):
        """The audio callback must not log (review finding
        wh-sounddevice-starvation-parity.3.2): logging takes handler locks and
        does I/O on a thread that must never block."""
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=0):
            with patch('shared_audio.microphone.elevate_current_thread',
                       return_value=True):
                mic = MicrophoneStream()
                with caplog.at_level("DEBUG", logger="shared_audio.microphone"):
                    for _ in range(3):
                        _invoke_callback(mic)
        assert caplog.records == []

    def test_first_read_logs_measured_priority(self, caplog):
        """The consumer thread reports the measured callback priority once."""
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=15):
            with patch('shared_audio.microphone.elevate_current_thread',
                       return_value=True):
                mic = MicrophoneStream()
                _invoke_callback(mic)
                with caplog.at_level("INFO", logger="shared_audio.microphone"):
                    mic.read(timeout=0.05)
                    mic.read(timeout=0.05)
        prio_lines = [r for r in caplog.records
                      if "Callback thread priority" in r.message]
        assert len(prio_lines) == 1
        assert "15" in prio_lines[0].message

    def test_first_read_logs_elevation_outcome_when_below(self, caplog):
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=0):
            with patch('shared_audio.microphone.elevate_current_thread',
                       return_value=True):
                mic = MicrophoneStream()
                _invoke_callback(mic)
                with caplog.at_level("INFO", logger="shared_audio.microphone"):
                    mic.read(timeout=0.05)
        prio_lines = [r for r in caplog.records
                      if "Callback thread priority" in r.message]
        assert len(prio_lines) == 1
        assert "elevated" in prio_lines[0].message
        assert "True" in prio_lines[0].message


class TestCallbackStallTracking:
    def _mic_with_tracker(self, clock):
        mic = MicrophoneStream()
        mic.stall_tracker = LoopStallTracker(
            stall_threshold_s=1.0, clock=clock, label="capture callback"
        )
        return mic

    def test_callback_gap_logs_stall_from_read(self, caplog):
        """The stall is detected in the callback but logged by read() on the
        consumer thread, so the callback never touches logging locks."""
        clock = FakeClock()
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=15):
            mic = self._mic_with_tracker(clock)
            with caplog.at_level("INFO", logger="shared_audio.microphone"):
                _invoke_callback(mic)
                clock.advance(2.5)
                _invoke_callback(mic)
                assert not any("[stall]" in r.message for r in caplog.records)
                mic.read(timeout=0.05)
        stall_lines = [r for r in caplog.records if "[stall]" in r.message]
        assert len(stall_lines) == 1
        assert stall_lines[0].levelname == "INFO"
        assert "2.5s" in stall_lines[0].message

    def test_normal_cadence_logs_no_stall(self, caplog):
        clock = FakeClock()
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=15):
            mic = self._mic_with_tracker(clock)
            with caplog.at_level("INFO", logger="shared_audio.microphone"):
                for _ in range(50):
                    _invoke_callback(mic)
                    clock.advance(0.03)
                    mic.read(timeout=0.05)
        assert not any("[stall]" in r.message for r in caplog.records)

    def test_stall_message_pins_queue_depth_value(self, caplog):
        """record() runs before the current frame is queued, so at the second
        callback exactly one frame (the first) is in the queue."""
        clock = FakeClock()
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=15):
            mic = self._mic_with_tracker(clock)
            with caplog.at_level("INFO", logger="shared_audio.microphone"):
                _invoke_callback(mic)
                clock.advance(2.0)
                _invoke_callback(mic)
                mic.read(timeout=0.05)
        stall_lines = [r for r in caplog.records if "[stall]" in r.message]
        assert len(stall_lines) == 1
        assert stall_lines[0].message == (
            "[stall] capture callback made no progress for 2.0s "
            "(likely whole-machine CPU starvation)"
            "; capture queue depth now 1"
        )

    def test_mic_has_stall_tracker_by_default(self):
        mic = MicrophoneStream()
        assert isinstance(mic.stall_tracker, LoopStallTracker)

    def test_start_resets_stall_tracker_so_pause_is_not_a_stall(self):
        """The stop()-to-start() pause is intentional; it must not count."""
        clock = FakeClock()
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=15):
            with patch('shared_audio.microphone.sd.InputStream') as MockStream:
                MockStream.return_value = Mock(active=False)
                mic = self._mic_with_tracker(clock)
                _invoke_callback(mic)
                mic.stop()
                clock.advance(60.0)
                mic.start()
                assert mic.stall_tracker.record() is None
                assert mic.stall_tracker.stall_count == 0


class TestCallbackLogHandoff:
    """The callback-to-consumer log handoff must not lose messages
    (wh-sounddevice-starvation-parity.3.5)."""

    def _mic_with_tracker(self, clock):
        mic = MicrophoneStream()
        mic.stall_tracker = LoopStallTracker(
            stall_threshold_s=1.0, min_log_interval_s=5.0, clock=clock,
            label="capture callback"
        )
        return mic

    def test_two_stalls_without_read_are_both_logged(self, caplog):
        """A second stall message must not overwrite an unread first one."""
        clock = FakeClock()
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=15):
            mic = self._mic_with_tracker(clock)
            with caplog.at_level("INFO", logger="shared_audio.microphone"):
                _invoke_callback(mic)
                clock.advance(2.0)
                _invoke_callback(mic)       # stall message A
                clock.advance(6.0)          # past the 5s rate limit
                _invoke_callback(mic)       # stall message B
                mic.read(timeout=0.05)
        stall_lines = [r for r in caplog.records if "[stall]" in r.message]
        assert len(stall_lines) == 2
        assert "2.0s" in stall_lines[0].message
        assert "6.0s" in stall_lines[1].message

    def test_pending_messages_survive_stop_start(self, caplog):
        """Diagnostics from the prior stream must still be logged after a
        restart, not discarded by start()."""
        clock = FakeClock()
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=15):
            with patch('shared_audio.microphone.sd.InputStream') as MockStream:
                MockStream.return_value = Mock(active=False)
                mic = self._mic_with_tracker(clock)
                _invoke_callback(mic)
                clock.advance(2.0)
                _invoke_callback(mic)       # stall message, never read
                mic.stop()
                mic.start()
                with caplog.at_level("INFO", logger="shared_audio.microphone"):
                    mic.read(timeout=0.05)
        stall_lines = [r for r in caplog.records if "[stall]" in r.message]
        assert len(stall_lines) == 1

    def test_one_read_drains_all_pending_messages(self, caplog):
        """read() drains the whole handoff, so a message stored while another
        is being logged cannot be erased -- the drain removes entries one by
        one and never clears the container wholesale."""
        clock = FakeClock()
        with patch('shared_audio.microphone.get_current_thread_priority',
                   return_value=0):
            with patch('shared_audio.microphone.elevate_current_thread',
                       return_value=True):
                mic = self._mic_with_tracker(clock)
                _invoke_callback(mic)       # priority line pending
                clock.advance(2.0)
                _invoke_callback(mic)       # stall message pending
                with caplog.at_level("INFO", logger="shared_audio.microphone"):
                    mic.read(timeout=0.05)
        messages = [r.message for r in caplog.records]
        assert any("Callback thread priority" in m for m in messages)
        assert any("[stall]" in m for m in messages)


class TestLoopStallTrackerLazyDepth:
    def test_callable_depth_not_evaluated_on_normal_iterations(self):
        """Queue depth costs a mutex acquisition; the tracker must only pay it
        when a rate-limited stall message actually forms."""
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        depth = Mock(return_value=3)
        for _ in range(10):
            tracker.record(depth)
            clock.advance(0.03)
        depth.assert_not_called()

    def test_callable_depth_evaluated_when_message_forms(self):
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        depth = Mock(return_value=3)
        tracker.record(depth)
        clock.advance(2.0)
        msg = tracker.record(depth)
        assert depth.call_count == 1
        assert msg is not None
        assert "capture queue depth now 3" in msg

    def test_callable_depth_not_evaluated_when_rate_limited(self):
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0,
                                   min_log_interval_s=5.0, clock=clock)
        depth = Mock(return_value=3)
        tracker.record(depth)
        clock.advance(2.0)
        tracker.record(depth)          # forms a message -> one evaluation
        clock.advance(2.0)
        assert tracker.record(depth) is None   # rate-limited -> no evaluation
        assert depth.call_count == 1

    def test_raising_callable_omits_depth_but_returns_message(self):
        """A diagnostic probe must never break the loop it observes
        (wh-sounddevice-starvation-parity.3.3): a raising depth callable is
        swallowed and the stall message forms without the depth suffix."""
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        depth = Mock(side_effect=RuntimeError("queue gone"))
        tracker.record(depth)
        clock.advance(2.0)
        msg = tracker.record(depth)
        assert msg == ("[stall] consumer loop made no progress for 2.0s "
                       "(likely whole-machine CPU starvation)")


class TestLoopStallTrackerLabel:
    def test_custom_label_appears_in_message(self):
        clock = FakeClock()
        tracker = LoopStallTracker(
            stall_threshold_s=1.0, clock=clock, label="capture callback"
        )
        tracker.record()
        clock.advance(2.0)
        msg = tracker.record()
        assert msg is not None
        assert "capture callback" in msg
        assert "consumer loop" not in msg

    def test_default_label_is_consumer_loop(self):
        """The google provider's [stall] line must stay byte-identical."""
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(2.0)
        msg = tracker.record(queue_depth=7)
        assert msg == ("[stall] consumer loop made no progress for 2.0s "
                      "(likely whole-machine CPU starvation)"
                      "; capture queue depth now 7")
