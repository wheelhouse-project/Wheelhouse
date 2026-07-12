"""Tests for shared_audio.overflow_monitor rate-limited logging.

Regression guard for the overflow log flood: when the audio consumer falls
behind real time the capture queue overflows continuously (observed ~5
events/second). The monitor previously logged two INFO lines per event, which
flooded the provider console and, once forwarded, would flood wheelhouse.log.
It must instead print at most one INFO summary per configured interval.
"""
import logging

from shared_audio.overflow_monitor import OverflowMonitor, OverflowConfig


class _Clock:
    """Controllable stand-in for time.time()."""

    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now


def _overflow_info_lines(records):
    return [
        r for r in records
        if r.levelno == logging.INFO and "[overflow]" in r.getMessage()
    ]


def test_flood_of_overflows_logs_single_info_summary(caplog, monkeypatch):
    """Many overflow events inside one interval produce one INFO summary line."""
    # Threshold high so no restart-trigger line appears; isolate the summary.
    monitor = OverflowMonitor(OverflowConfig(overflow_threshold=1000))
    caplog.set_level(logging.INFO, logger="shared_audio.overflow_monitor")

    clock = _Clock(1000.0)
    monkeypatch.setattr("shared_audio.overflow_monitor.time.time", clock)
    # Freeze monotonic too so all 50 events fall inside one summary interval.
    monkeypatch.setattr("shared_audio.overflow_monitor.time.monotonic", _Clock(5000.0))

    for _ in range(50):
        monitor.report_overflow()

    info_lines = _overflow_info_lines(caplog.records)
    assert len(info_lines) == 1, (
        f"expected a single INFO summary for 50 overflows, got {len(info_lines)}"
    )
    assert "in the last" in info_lines[0].getMessage(), (
        "the single INFO line should be the periodic summary"
    )


def test_summary_repeats_after_interval_elapses(caplog, monkeypatch):
    """A new INFO summary is printed once the summary interval has passed."""
    monitor = OverflowMonitor(
        OverflowConfig(overflow_threshold=1000, log_summary_interval_seconds=10.0)
    )
    caplog.set_level(logging.INFO, logger="shared_audio.overflow_monitor")

    # The summary interval is measured on the monotonic clock; drive it.
    mono = _Clock(1000.0)
    monkeypatch.setattr("shared_audio.overflow_monitor.time.time", _Clock(1000.0))
    monkeypatch.setattr("shared_audio.overflow_monitor.time.monotonic", mono)

    monitor.report_overflow()   # summary #1 at t=1000
    mono.now = 1005.0
    monitor.report_overflow()   # within interval -> suppressed
    mono.now = 1011.0
    monitor.report_overflow()   # >10s after #1 -> summary #2

    assert len(_overflow_info_lines(caplog.records)) == 2


def test_summary_survives_backward_wall_clock_step(caplog, monkeypatch):
    """A backward wall-clock step must not suppress the INFO summary.

    The summary interval must be measured on a monotonic clock. time.time() can
    jump backward (an NTP correction, a manual clock change); measuring the
    interval on it would silence the summary for an unbounded period while
    frames keep dropping -- the exact signal this logging exists to preserve.
    """
    monitor = OverflowMonitor(
        OverflowConfig(overflow_threshold=1000, log_summary_interval_seconds=10.0)
    )
    caplog.set_level(logging.INFO, logger="shared_audio.overflow_monitor")

    wall = _Clock(1000.0)
    mono = _Clock(5000.0)
    monkeypatch.setattr("shared_audio.overflow_monitor.time.time", wall)
    monkeypatch.setattr("shared_audio.overflow_monitor.time.monotonic", mono)

    monitor.report_overflow()          # summary #1
    # Wall clock jumps far backward (e.g. an NTP correction); the monotonic
    # clock advances past the interval as it always does.
    wall.now = 100.0
    mono.now = 5011.0
    monitor.report_overflow()          # summary #2 -- the gate must read monotonic

    assert len(_overflow_info_lines(caplog.records)) == 2


def test_first_summary_logs_even_near_monotonic_zero(caplog, monkeypatch):
    """The first overflow always logs a summary, even when the monotonic clock
    reads less than the interval at that moment.

    time.monotonic() has an arbitrary reference point; on some platforms it is
    small shortly after boot. Initializing the last-summary time to 0.0 would
    suppress the very first summary when monotonic() < the interval. The first
    report must be unconditional.
    """
    monitor = OverflowMonitor(
        OverflowConfig(overflow_threshold=1000, log_summary_interval_seconds=10.0)
    )
    caplog.set_level(logging.INFO, logger="shared_audio.overflow_monitor")

    # Monotonic reads 0.0 on the first report -- e.g. right after boot.
    monkeypatch.setattr("shared_audio.overflow_monitor.time.time", _Clock(1000.0))
    monkeypatch.setattr("shared_audio.overflow_monitor.time.monotonic", _Clock(0.0))

    monitor.report_overflow()

    assert len(_overflow_info_lines(caplog.records)) == 1


def test_triggering_restart_still_logged_at_info(caplog, monkeypatch):
    """The rare 'restart triggered' line stays at INFO and is not suppressed."""
    monitor = OverflowMonitor(
        OverflowConfig(overflow_threshold=5, restart_cooldown_seconds=60.0)
    )
    caplog.set_level(logging.INFO, logger="shared_audio.overflow_monitor")

    clock = _Clock(1000.0)
    monkeypatch.setattr("shared_audio.overflow_monitor.time.time", clock)

    for _ in range(5):
        monitor.report_overflow()

    triggering = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "TRIGGERING RESTART" in r.getMessage()
    ]
    assert len(triggering) == 1, "restart trigger must remain visible at INFO"


def test_summary_reaches_handler_on_module_logger(monkeypatch):
    """The summary is emitted on the 'shared_audio.overflow_monitor' logger.

    The provider forwards this to wheelhouse.log by attaching a handler to that
    exact logger name; this guards the name so the forwarding cannot silently
    break if the module moves.
    """
    received = []

    class _Capture(logging.Handler):
        def emit(self, record):
            received.append(record.getMessage())

    handler = _Capture()
    handler.setLevel(logging.INFO)
    target = logging.getLogger("shared_audio.overflow_monitor")
    original_level = target.level
    # Mirror the provider, which configures INFO-level logging (basicConfig).
    target.setLevel(logging.INFO)
    target.addHandler(handler)
    try:
        monitor = OverflowMonitor(OverflowConfig(overflow_threshold=1000))
        clock = _Clock(1000.0)
        monkeypatch.setattr("shared_audio.overflow_monitor.time.time", clock)
        monkeypatch.setattr("shared_audio.overflow_monitor.time.monotonic", _Clock(5000.0))
        monitor.report_overflow()
        assert any("in the last" in m for m in received), (
            "summary must be logged on the shared_audio.overflow_monitor logger"
        )
    finally:
        target.removeHandler(handler)
        target.setLevel(original_level)


def _summary_lines(records):
    """Only the periodic summary line, not the reset/restart INFO lines."""
    return [
        r for r in records
        if r.levelno == logging.INFO and "in the last" in r.getMessage()
    ]


def test_reset_for_restart_reactivates_first_summary(caplog, monkeypatch):
    """After reset_for_restart(), the next overflow logs a summary immediately.

    reset_for_restart() clears the overflow tracking after a restart completes;
    the rate-limit gate must reset with it, so the first overflow after a restart
    always logs -- the same "first overflow always logs" property the monitor
    guarantees at startup. Without this, a restart that happens within one summary
    interval of the last summary would silence the first post-restart overflow for
    the rest of the interval, exactly when an operator needs to know the restart
    did not resolve the overflow.
    """
    monitor = OverflowMonitor(
        OverflowConfig(overflow_threshold=1000, log_summary_interval_seconds=10.0)
    )
    caplog.set_level(logging.INFO, logger="shared_audio.overflow_monitor")

    monkeypatch.setattr("shared_audio.overflow_monitor.time.time", _Clock(1000.0))
    mono = _Clock(5000.0)
    monkeypatch.setattr("shared_audio.overflow_monitor.time.monotonic", mono)

    monitor.report_overflow()       # summary #1 at monotonic 5000
    monitor.reset_for_restart()     # restart completed -> gate must reset
    mono.now = 5003.0               # only 3s later, well inside the 10s interval
    monitor.report_overflow()       # must still log summary #2

    assert len(_summary_lines(caplog.records)) == 2, (
        "reset_for_restart() must reset the summary rate-limit gate so the first "
        "post-restart overflow logs even within one interval of the prior summary"
    )
