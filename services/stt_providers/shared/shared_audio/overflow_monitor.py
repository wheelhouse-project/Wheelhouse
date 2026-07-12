"""PortAudio Overflow Detection and Automatic Restart System

This module monitors PortAudio input overflow events and triggers automatic
service restart when persistent overflow patterns are detected. This prevents
the STT service from becoming unresponsive due to audio buffer issues.

Key Classes:
  - OverflowMonitor: Tracks overflow frequency and triggers restart when needed

Key Features:
  - Sliding window overflow tracking
  - Configurable thresholds to distinguish temporary vs persistent issues
  - Cooldown protection to prevent restart loops
  - Graceful restart coordination with main STT loop

Typical Usage:
  overflow_monitor = OverflowMonitor(config)

  # In audio callback when status indicates overflow:
  if overflow_monitor.report_overflow():
      # Trigger restart signal
      restart_needed = True
"""
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Callable

logger = logging.getLogger(__name__)


@dataclass
class OverflowConfig:
    """Configuration for overflow detection and restart behavior."""
    # How many overflows in the window trigger restart
    overflow_threshold: int = 5
    # Time window in seconds to track overflows
    window_seconds: float = 30.0
    # Minimum time between restart attempts (prevents loops)
    restart_cooldown_seconds: float = 60.0
    # Maximum restart attempts before giving up
    max_restart_attempts: int = 3
    # Reset attempt counter after this many seconds of stable operation
    stable_reset_seconds: float = 300.0  # 5 minutes
    # Minimum seconds between INFO summary lines. Individual overflow events are
    # logged at DEBUG only; at INFO they flood the log (this is called several
    # times per second during a sustained overflow).
    log_summary_interval_seconds: float = 10.0


class OverflowMonitor:
    """Monitors PortAudio overflow events and triggers restart when needed."""

    def __init__(self, config: OverflowConfig, restart_callback: Optional[Callable] = None):
        self.config = config
        self.restart_callback = restart_callback

        # Overflow tracking
        self.overflow_times = deque()  # Store timestamps of overflow events

        # Restart management
        self.last_restart_time = 0.0
        self.restart_attempts = 0
        self.service_start_time = time.time()

        # State tracking
        self.restart_requested = False

        # Monotonic timestamp of the last INFO summary line, for rate-limiting
        # (see log_summary_interval_seconds). None until the first summary, so
        # the first overflow always logs regardless of the monotonic clock's
        # arbitrary reference point (it can read < the interval just after boot).
        self._last_summary_time = None

    def report_overflow(self, context: dict = None) -> bool:
        """
        Report an overflow event and check if restart is needed.

        Args:
            context: Optional dict with state at overflow time for diagnostics

        Returns:
            True if restart should be triggered, False otherwise
        """
        current_time = time.time()

        # Add this overflow to our tracking
        self.overflow_times.append(current_time)

        # Clean old overflow events outside our window
        window_start = current_time - self.config.window_seconds
        while self.overflow_times and self.overflow_times[0] < window_start:
            self.overflow_times.popleft()

        overflow_count = len(self.overflow_times)

        # Per-event detail at DEBUG only. At INFO this floods the log: a
        # sustained overflow calls this several times per second.
        logger.debug(f"[overflow] Detected overflow event ({overflow_count}/{self.config.overflow_threshold} in {self.config.window_seconds}s window)")
        if context:
            logger.debug(f"[overflow] Context: {context}")

        # Rate-limited INFO summary so an ongoing overflow stays visible (and can
        # be forwarded to wheelhouse.log) without one line per dropped frame.
        # Measure the interval on a monotonic clock: time.time() can jump
        # backward (an NTP correction, a manual clock change), which would
        # suppress the summary for an unbounded period while frames keep
        # dropping -- the exact signal this line exists to preserve.
        summary_now = time.monotonic()
        if (self._last_summary_time is None
                or summary_now - self._last_summary_time >= self.config.log_summary_interval_seconds):
            logger.info(
                f"[overflow] {overflow_count} audio input overflows in the last "
                f"{self.config.window_seconds:.0f}s -- frames are being dropped "
                f"(audio consumer behind real time)"
            )
            self._last_summary_time = summary_now

        # Check if we've exceeded threshold
        if overflow_count >= self.config.overflow_threshold:
            return self._should_restart(current_time)

        return False

    def _should_restart(self, current_time: float) -> bool:
        """Determine if restart should be triggered based on current conditions."""

        # Check cooldown period
        time_since_last_restart = current_time - self.last_restart_time
        if time_since_last_restart < self.config.restart_cooldown_seconds:
            # DEBUG: repeats on every overflow while inside the cooldown window.
            logger.debug(f"[overflow] Restart needed but still in cooldown ({time_since_last_restart:.1f}s < {self.config.restart_cooldown_seconds}s)")
            return False

        # Check if we've exceeded max attempts
        if self.restart_attempts >= self.config.max_restart_attempts:
            # DEBUG: repeats on every overflow once the attempt cap is reached.
            logger.debug(f"[overflow] Restart needed but max attempts reached ({self.restart_attempts}/{self.config.max_restart_attempts})")
            return False

        # Check if enough time has passed since service start to reset attempt counter
        time_since_start = current_time - self.service_start_time
        if time_since_start > self.config.stable_reset_seconds:
            logger.info(f"[overflow] Resetting restart attempt counter after {time_since_start:.1f}s of stable operation")
            self.restart_attempts = 0
            self.service_start_time = current_time

        # All checks passed - trigger restart
        self.last_restart_time = current_time
        self.restart_attempts += 1
        self.restart_requested = True

        logger.info(f"[overflow] TRIGGERING RESTART (attempt {self.restart_attempts}/{self.config.max_restart_attempts})")

        # Call restart callback if provided
        if self.restart_callback:
            self.restart_callback()

        return True

    def reset_for_restart(self):
        """Reset state after restart has been completed."""
        self.overflow_times.clear()
        self.restart_requested = False
        # Reset the summary rate-limit gate too. A restart starts the overflow
        # tracking fresh, so the "first overflow always logs" property (see the
        # None init in __init__) must hold again: without this, a restart that
        # happens within one summary interval of the last summary would suppress
        # the first post-restart summary for the rest of the interval -- exactly
        # when the operator needs to know whether the restart resolved the drops.
        self._last_summary_time = None
        logger.info("[overflow] Monitor state reset after restart")

    def get_status(self) -> dict:
        """Get current overflow monitoring status for debugging."""
        current_time = time.time()
        return {
            'overflow_count_current_window': len(self.overflow_times),
            'threshold': self.config.overflow_threshold,
            'window_seconds': self.config.window_seconds,
            'restart_attempts': self.restart_attempts,
            'max_attempts': self.config.max_restart_attempts,
            'time_since_last_restart': current_time - self.last_restart_time,
            'cooldown_seconds': self.config.restart_cooldown_seconds,
            'restart_requested': self.restart_requested
        }
