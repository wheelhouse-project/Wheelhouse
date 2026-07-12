"""Idle detection plugin for automatic speech transcription control.

Monitors system idle time using Windows GetLastInputInfo API and publishes
idle state changes to EventBus. Enables automatic speech transcription
suppression when user is away from computer.

Configuration:
    [plugins.idle_monitor]
    enabled = true
    idle_timeout_minutes = 5
    polling_interval_seconds = 10

Events Published:
    - SystemIdleStateChangedEvent: When system becomes idle or active

Integration:
    StateManager subscribes to idle events and suppresses speech transcription
    when system is idle, restoring previous state when activity resumes.
"""
import asyncio
import ctypes
import logging
import time
from ctypes import wintypes, Structure, byref
from typing import TYPE_CHECKING

from services.wheelhouse.plugins.base import BasePlugin, PluginState
from services.wheelhouse.events import SystemIdleStateChangedEvent

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.event_bus import EventBus

logger = logging.getLogger(__name__)


class LASTINPUTINFO(Structure):
    """Windows API structure for GetLastInputInfo."""
    _fields_ = [
        ('cbSize', wintypes.UINT),
        ('dwTime', wintypes.DWORD),
    ]


class IdleMonitorPlugin(BasePlugin):
    """Monitors system idle state and publishes state change events.

    Uses Windows GetLastInputInfo API to track time since last user input
    (keyboard or mouse). When idle duration exceeds configured threshold,
    publishes SystemIdleStateChangedEvent to trigger speech suppression.
    """

    def __init__(self):
        super().__init__()
        self._config = None
        self._event_bus = None
        self._monitor_task = None

        # Configuration
        self.idle_timeout_minutes = 5
        self.polling_interval_seconds = 10

        # State tracking
        self._is_idle = False
        self._last_state_change_time = None
        self._consecutive_errors = 0
        self._max_consecutive_errors = 3

    @property
    def name(self) -> str:
        """Return plugin identifier for registration.
        
        Returns:
            str: Plugin name 'idle_monitor'
        """
        return "idle_monitor"

    async def initialize(self, config: "ConfigService", event_bus: "EventBus") -> None:
        """Initialize with configuration.
        
        Args:
            config: ConfigService for timeout and polling settings
            event_bus: EventBus for publishing idle state changes
        """
        self._config = config
        self._event_bus = event_bus

        # Load configuration
        self.idle_timeout_minutes = config.get(
            "plugins.idle_monitor.idle_timeout_minutes",
            5
        )
        self.polling_interval_seconds = config.get(
            "plugins.idle_monitor.polling_interval_seconds",
            10
        )

        # Validate configuration
        if self.idle_timeout_minutes <= 0:
            raise ValueError("idle_timeout_minutes must be positive")
        if self.polling_interval_seconds <= 0:
            raise ValueError("polling_interval_seconds must be positive")

        # Warn if polling is too aggressive
        if self.polling_interval_seconds < 5:
            logger.warning(
                f"Polling interval {self.polling_interval_seconds}s is aggressive. "
                "Consider increasing to reduce CPU usage."
            )

        self._state = PluginState.INITIALIZED
        logger.info(
            f"IdleMonitor initialized: timeout={self.idle_timeout_minutes}min, "
            f"poll={self.polling_interval_seconds}s"
        )

    async def start(self) -> None:
        """:flow: Speech Suppression by Idle
        :step: 1
        :description: Plugin startup - validates Windows API and launches monitoring task
        :data_in: Configuration values (idle_timeout_minutes, polling_interval_seconds)
        :data_out: Running monitoring task in background
        :notes: Entry point for idle detection flow. Tests GetLastInputInfo API availability before starting. Launches async monitoring task (step 2) that runs continuously until plugin stops. If API unavailable, plugin enters FAILED state and flow terminates. No suppression occurs until threshold crossed in monitoring loop.
        """
        try:
            self._state = PluginState.STARTING

            # Test API availability
            try:
                _ = self._get_idle_duration_seconds()
            except Exception as e:
                logger.error(f"GetLastInputInfo API not available: {e}")
                self._state = PluginState.FAILED
                return

            # Start monitoring task
            self._monitor_task = asyncio.create_task(self._monitor_loop())

            self._state = PluginState.RUNNING
            logger.info("IdleMonitor started")

        except Exception as e:
            logger.error(f"IdleMonitor failed to start: {e}", exc_info=True)
            self._state = PluginState.FAILED

    async def stop(self) -> None:
        """Stop idle monitoring.
        
        Cancels monitoring task and waits for graceful shutdown.
        """
        self._state = PluginState.STOPPING

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        self._state = PluginState.STOPPED
        logger.info("IdleMonitor stopped")

    def get_health_status(self) -> dict:
        """Return current health status.
        
        Returns:
            dict: Status with idle state, thresholds, and error count
        """
        status = "healthy"
        if self._state != PluginState.RUNNING:
            status = "unhealthy"
        elif self._consecutive_errors > 0:
            status = "degraded"

        return {
            "status": status,
            "state": self._state.value,
            "is_idle": self._is_idle,
            "idle_timeout_minutes": self.idle_timeout_minutes,
            "polling_interval_seconds": self.polling_interval_seconds,
            "consecutive_errors": self._consecutive_errors,
            "last_state_change": self._last_state_change_time
        }

    def _get_idle_duration_seconds(self) -> float:
        """Get seconds since last user input via Windows API.

        Returns:
            float: Seconds since last keyboard/mouse input

        Raises:
            RuntimeError: If Windows API call fails
        """
        lastInputInfo = LASTINPUTINFO()
        lastInputInfo.cbSize = ctypes.sizeof(lastInputInfo)

        if not ctypes.windll.user32.GetLastInputInfo(byref(lastInputInfo)):
            raise RuntimeError("GetLastInputInfo failed")

        # GetTickCount can wrap after ~49 days, but the difference calculation
        # still works correctly due to unsigned arithmetic
        millis = ctypes.windll.kernel32.GetTickCount() - lastInputInfo.dwTime
        return millis / 1000.0

    async def _monitor_loop(self):
        """:flow: Speech Suppression by Idle
        :step: 2
        :description: Continuous polling loop monitoring system idle duration via Windows API
        :data_in: Idle threshold (from config), polling interval (from config)
        :data_out: SystemIdleStateChangedEvent published to EventBus on state transitions
        :notes: Core monitoring logic. Polls GetLastInputInfo every N seconds (default 10s) to check time since last user input. Compares duration to threshold (default 5min). On idle/active state change, publishes SystemIdleStateChangedEvent to EventBus for StateManager (step 3). Only publishes on transitions - not continuous heartbeats - to reduce event spam. Handles API errors with consecutive error counter, fails plugin after 3 consecutive failures to prevent infinite retry loops.
        """
        logger.info("IdleMonitor monitoring loop started")

        idle_threshold_seconds = self.idle_timeout_minutes * 60

        while self._state == PluginState.RUNNING:
            try:
                # Get current idle duration
                idle_duration = self._get_idle_duration_seconds()

                # Determine current state
                currently_idle = idle_duration >= idle_threshold_seconds

                # Detect state changes
                if currently_idle != self._is_idle:
                    # State changed
                    self._is_idle = currently_idle
                    self._last_state_change_time = time.time()

                    # Publish event
                    event = SystemIdleStateChangedEvent(
                        is_idle=currently_idle,
                        idle_duration_seconds=idle_duration
                    )
                    await self._event_bus.publish(event)

                    if currently_idle:
                        logger.info(
                            f"System became IDLE after {idle_duration:.1f}s "
                            f"(threshold: {idle_threshold_seconds}s)"
                        )
                    else:
                        logger.info("System became ACTIVE")

                # Reset error counter on success
                self._consecutive_errors = 0

            except Exception as e:
                self._consecutive_errors += 1
                logger.error(
                    f"Error checking idle state (#{self._consecutive_errors}): {e}"
                )

                # If too many consecutive errors, fail the plugin
                if self._consecutive_errors >= self._max_consecutive_errors:
                    logger.error(
                        f"Too many consecutive errors ({self._consecutive_errors}). "
                        "Failing plugin."
                    )
                    self._state = PluginState.FAILED
                    break

            # Sleep until next check
            await asyncio.sleep(self.polling_interval_seconds)

        logger.info("IdleMonitor monitoring loop exited")
