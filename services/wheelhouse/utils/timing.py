"""Performance timing and measurement utilities.

This module provides utilities for measuring and logging performance metrics
throughout the WheelHouse application. It implements standardized performance
logging with step-by-step timing analysis and total elapsed time tracking
for comprehensive performance monitoring.

Key Functions:
  - log_perf_time: Logs performance metrics with step and total timing.

Key Features:
  - Step-by-step performance measurement with millisecond precision
  - Total elapsed time tracking for end-to-end operation analysis
  - Standardized performance log formatting for consistent analysis
  - High-resolution timing using time.perf_counter()
  - Structured performance data for automated analysis

Timing Methodology:
  - Uses time.perf_counter() for high-resolution, monotonic timing
  - Measures both individual step duration and cumulative elapsed time
  - Provides millisecond precision for detailed performance analysis
  - Consistent log format for automated performance log parsing

Performance Analysis:
  - Step duration: Time spent in individual operation
  - Total elapsed: Cumulative time from operation start
  - Formatted output with aligned columns for readability
  - Integration with standard logging system

Typical Usage:
  from utils.timing import log_perf_time
  import time
  
  initial_time = time.perf_counter()
  
  # Perform operation
  step_start = time.perf_counter()
  perform_operation()
  log_perf_time("Operation completed", step_start, initial_time)
  
  # Next step
  step_start = time.perf_counter()
  next_operation()
  log_perf_time("Next operation done", step_start, initial_time)
"""
# utils/timing.py
import logging
import time

logger = logging.getLogger(__name__)

def log_perf_time(message: str, start_time: float, initial_time: float):
    """
    Logs the performance time of a step and the total elapsed time.

    Args:
        message (str): A descriptive message for the step being timed.
        start_time (float): The time.perf_counter() value from the start of this specific step.
        initial_time (float): The time.perf_counter() value from the very beginning of the entire process.
    """
    end_time = time.perf_counter()
    step_duration = (end_time - start_time) * 1000  # in milliseconds
    total_elapsed = (end_time - initial_time) * 1000 # in milliseconds
    
    logger.info(f"PERF: {message:<40} | Step: {step_duration:8.2f} ms | Total: {total_elapsed:8.2f} ms")