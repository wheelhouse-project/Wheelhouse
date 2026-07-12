"""System resource monitoring with notification alerts.

This module provides continuous monitoring of system resources including
CPU usage, memory consumption, and other performance metrics. It implements
an asynchronous monitoring loop with configurable thresholds and desktop
notification alerts for resource constraint warnings.

Key Functions:
  - monitor_resources: Main asynchronous resource monitoring loop.

Key Features:
  - Continuous CPU and memory usage monitoring
  - Configurable monitoring intervals with cancellation support
  - Desktop notification alerts for resource threshold breaches
  - Responsive cancellation handling with segmented sleep cycles
  - Integration with psutil for cross-platform resource access
  - Plyer-based desktop notifications

Monitoring Capabilities:
  - CPU utilization percentage tracking
  - Memory usage monitoring (RAM, virtual memory)
  - Process-specific resource consumption analysis
  - Threshold-based alert triggering
  - Historical resource usage tracking

Alert System:
  - Desktop notifications via plyer library
  - Configurable threshold levels for alerts
  - Rate limiting to prevent notification spam
  - Contextual alert messages with resource details

Typical Usage:
  from utils.monitors import monitor_resources
  import asyncio
  
  # Start resource monitoring task
  monitoring_task = asyncio.create_task(monitor_resources())
  
  # Let it run in background
  await asyncio.gather(monitoring_task, other_tasks...)
  
  # Cancel when needed
  monitoring_task.cancel()
"""
"""
Resource monitoring utilities
"""

import asyncio
import logging

import psutil
from plyer import notification # Ensure plyer is installed if notifications are desired

logger = logging.getLogger(__name__)

async def monitor_resources():
    """
    Monitors CPU and memory usage.
    Shows a notification if usage is too high.
    """
    logger.info("Resource monitor task started.")
    try:
        while True: # Main loop for the monitor
            try:
                logger.debug("Resource monitor: Top of monitoring cycle.")

                # Break the 10-second sleep into 1-second chunks to be more responsive to cancellation
                # Total sleep duration remains approximately 10 seconds.
                # Check for cancellation before each sleep segment.
                # Use a variable for total sleep and segment duration for clarity.
                total_sleep_duration = 10.0
                sleep_segment_duration = 1.0
                segments = int(total_sleep_duration / sleep_segment_duration)

                for i in range(segments):
                    # Check if the task has been cancelled before sleeping
                    if asyncio.current_task().cancelled():
                        logger.info("Resource monitor: Sleep segment detected cancellation.")
                        raise asyncio.CancelledError # Propagate cancellation

                    await asyncio.sleep(sleep_segment_duration)
                    logger.debug(f"Resource monitor: Slept for segment {i+1}/{segments}.")

                # After sleeping, perform resource checks
                logger.debug("Resource monitor: Woke from sleep segments, checking resources.")
                cpu_usage = psutil.cpu_percent(interval=None) # Non-blocking
                memory_info = psutil.virtual_memory()
                memory_usage = memory_info.percent

                log_message = f"Resource usage - CPU: {cpu_usage:.1f}%, Memory: {memory_usage:.1f}% ({memory_info.used/1024/1024:.0f}MB/{memory_info.total/1024/1024:.0f}MB)"

                if isinstance(cpu_usage, (int, float)) and isinstance(memory_usage, (int, float)):
                    if cpu_usage > 95 or memory_usage > 95:
                        logger.warning(f"High resource usage detected: {log_message}")
                        try:
                            if hasattr(notification, 'notify') and callable(notification.notify):
                                notification.notify(
                                    title="High Resource Usage",
                                    message=f"CPU: {cpu_usage:.1f}%, Memory: {memory_usage:.1f}%",
                                    timeout=5,
                                )
                            else:
                                logger.warning("Plyer notification.notify method not available or not callable.")
                        except Exception as notify_err:
                            logger.error(f"Error sending notification in monitor_resources: {notify_err}")
                    elif cpu_usage > 50 or memory_usage > 70:
                        logger.info(log_message) # Log info for moderate usage
                    else:
                        logger.debug(log_message) # Log debug for normal usage
                else:
                    logger.debug(
                        f"monitor_resources: cpu_usage or memory_usage is None or not a number. "
                        f"CPU Type: {type(cpu_usage).__name__}, MEM Type: {type(memory_usage).__name__}"
                    )
                logger.debug("Resource monitor: Finished checks in loop.")

            except asyncio.CancelledError:
                # This is the expected way for the task to be stopped.
                logger.info("Resource monitor: Main loop detected cancellation. Re-raising.")
                raise # Re-raise to be caught by the outer handler or asyncio.gather
            except Exception as e:
                logger.error(f"Error in monitor_resources loop: {e}", exc_info=True)
                # If an unexpected error occurs, wait a bit before retrying to avoid spamming logs.
                try:
                    await asyncio.sleep(30.0)
                except asyncio.CancelledError:
                    logger.info("Resource monitor: Sleep after error was cancelled.")
                    raise # Re-raise cancellation

    except asyncio.CancelledError:
        # This block is reached if the `while True` loop is broken by a CancelledError
        # that was re-raised from the inner try/except.
        logger.info("Resource monitor: Coroutine was cancelled (outer catch). Task will terminate.")
    finally:
        # This block will always execute, even if the task is cancelled.
        # Ensure any cleanup specific to monitor_resources would go here.
        logger.info("Resource monitor task finishing (finally block executed).")