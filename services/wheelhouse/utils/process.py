"""Process management and system utilities.

This module provides comprehensive process management capabilities including
process discovery, termination, window enumeration, and system resource
monitoring. It implements safe process operations with error handling and
logging for reliable system integration.

Key Functions:
  - Various process management and system utility functions.
  - Window enumeration and process discovery.
  - Safe process termination with timeout handling.

Key Features:
  - Process discovery by name, PID, and window title
  - Safe process termination with graceful shutdown
  - Window enumeration and handle management
  - System resource monitoring and process tracking
  - Error handling for process access and permission issues
  - Integration with Windows process and window APIs

Process Operations:
  - Process enumeration and filtering
  - Graceful process shutdown with timeout
  - Process tree management and child process handling
  - Window-to-process mapping and resolution
  - Process resource usage monitoring

Safety Features:
  - Permission-aware process operations
  - Timeout handling for hanging processes
  - Error logging and graceful degradation
  - Resource cleanup and handle management

Typical Usage:
  from utils.process import get_pid_file_path, write_pid_file
  
  # Manage process PID file
  write_pid_file()
  
  # Clean up on shutdown
  clear_pid_file()
"""
import logging
import os
import psutil
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)

def get_pid_file_path() -> str:
    """Gets the application-specific path for the PID file."""
    # Using a common application data folder is more robust.
    app_data_path = os.getenv('APPDATA') or os.path.expanduser('~')
    pid_dir = os.path.join(app_data_path, 'WheelHouse')
    os.makedirs(pid_dir, exist_ok=True)
    return os.path.join(pid_dir, 'wheelhouse.pid')

def write_pid_file():
    """Writes the current process ID to the PID file."""
    pid_file = get_pid_file_path()
    try:
        with open(pid_file, 'w') as f:
            f.write(str(os.getpid()))
        logger.info(f"Process {os.getpid()} registered in PID file: {pid_file}")
    except IOError as e:
        logger.critical(f"Unable to write to PID file {pid_file}: {e}. Application cannot start.", exc_info=True)
        sys.exit(1)

def clear_pid_file():
    """
    Deletes the PID file. This should be called at the very end of a clean shutdown.
    """
    pid_file = get_pid_file_path()
    try:
        if os.path.exists(pid_file):
            os.remove(pid_file)
            logger.info(f"PID file {pid_file} removed.")
    except (IOError, ValueError) as e:
        logger.error(f"Error clearing PID file {pid_file}: {e}")

def manage_process_instance():
    """
    Ensures only one instance of the application is running by finding and
    terminating the previous instance using the PID file.
    """
    pid_file = get_pid_file_path()
    current_pid = os.getpid()
    logger.info(f"Instance manager running in PID {current_pid}. Checking for existing instances...")

    if not os.path.exists(pid_file):
        logger.info("No existing PID file found. Assuming clean start.")
        return

    try:
        with open(pid_file, 'r') as f:
            old_pid = int(f.read().strip())

        if old_pid == current_pid:
            logger.warning("PID file contains our own PID. This is unexpected but not fatal.")
            return

        if psutil.pid_exists(old_pid):
            logger.warning(f"Found existing Wheelhouse instance from PID file: {old_pid}. Terminating process group...")
            try:
                parent = psutil.Process(old_pid)
                children = parent.children(recursive=True)
                
                # Terminate children first
                for child in children:
                    logger.debug(f"Terminating child process {child.pid} of old instance.")
                    child.terminate()

                # Terminate the parent process
                logger.debug(f"Terminating parent process {parent.pid} of old instance.")
                parent.terminate()
                
                # Wait for all to die
                gone, alive = psutil.wait_procs(children + [parent], timeout=5)
                if alive:
                    for p in alive:
                        logger.warning(f"Process {p.pid} did not terminate gracefully. Killing.")
                        p.kill()
                logger.info("Previous instance terminated.")

            except psutil.NoSuchProcess:
                logger.info(f"Process {old_pid} from PID file disappeared before termination.")
            except Exception as e:
                logger.error(f"Error terminating process {old_pid}: {e}", exc_info=True)
        else:
            logger.info(f"Stale PID file found for non-existent process {old_pid}. Ignoring.")

    except (IOError, ValueError) as e:
        logger.warning(f"Could not read or parse stale PID file: {e}")
    
    # Clean up the old file regardless of outcome
    clear_pid_file()