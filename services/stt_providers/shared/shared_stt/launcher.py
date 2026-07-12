"""Shared launcher base for STT process supervision.

This module provides the core launcher logic that can be used by all STT providers.
It handles:
- Process supervision with crash detection
- Flag file restart mechanism
- Configurable crash thresholds
- Graceful shutdown on SIGINT/SIGTERM

Typical Usage:
    from shared.stt.launcher import run_launcher, LauncherConfig

    config = LauncherConfig(
        app_name="GoogleSTT",
        main_script="main.py"
    )
    run_launcher(config)
"""
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Global for signal handler
_subprocess: Optional[subprocess.Popen] = None


@dataclass
class LauncherConfig:
    """Configuration for the launcher.

    Attributes:
        app_name: Name of the application (used for PID file, logging)
        main_script: Path to the main script to run
        crash_threshold_s: If process exits faster than this, it's a crash
        max_crashes: Abort after this many consecutive crashes
        launcher_dir: Directory containing the launcher (auto-detected if None)
        forward_args: Extra arguments to forward to main.py (e.g., ["--ws-port", "<port>"])
    """
    app_name: str
    main_script: str
    crash_threshold_s: float = 15.0
    max_crashes: int = 3
    launcher_dir: Optional[str] = None
    forward_args: Optional[list[str]] = None


def get_app_data_path() -> str:
    """Get the application data directory path.

    Returns platform-specific path:
    - Windows: %APPDATA%/WheelHouse
    - Linux/Mac: ~/.wheelhouse
    """
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "WheelHouse")
    else:
        return os.path.expanduser("~/.wheelhouse")


def get_pid_file_path(app_name: str) -> str:
    """Get the PID file path for the given app name.

    Args:
        app_name: Application name (e.g., "GoogleSTT", "Zipformer")

    Returns:
        Full path to the PID file
    """
    app_data_path = get_app_data_path()
    os.makedirs(app_data_path, exist_ok=True)
    return os.path.join(app_data_path, f"{app_name.lower()}.pid")


def get_restart_flag_path(app_name: str) -> str:
    """Get the restart flag file path for the given app name.

    Args:
        app_name: Application name (e.g., "GoogleSTT", "Zipformer")

    Returns:
        Full path to the restart flag file
    """
    app_data_path = get_app_data_path()
    os.makedirs(app_data_path, exist_ok=True)
    return os.path.join(app_data_path, f"{app_name.lower()}.restart")


def should_restart(
    exit_code: int,
    uptime: float,
    restart_flag_exists: bool,
    crash_threshold_s: float
) -> bool:
    """Determine if the process should be restarted.

    Decision logic:
    1. Restart flag exists -> restart (intentional restart request)
    2. Exit code 0 -> do NOT restart (clean shutdown via shutdown command)
    3. Short uptime + non-zero exit -> restart (crash)
    4. Long uptime + non-zero exit -> do not restart (normal exit)

    Args:
        exit_code: The process exit code
        uptime: How long the process ran in seconds
        restart_flag_exists: Whether the restart flag file exists
        crash_threshold_s: Threshold in seconds to consider a crash

    Returns:
        True if the process should be restarted
    """
    if restart_flag_exists:
        return True
    elif exit_code == 0:
        # Clean shutdown - exit code 0 means "do not restart"
        return False
    elif uptime < crash_threshold_s:
        # Process crashed quickly
        return True
    else:
        # Normal exit with non-zero code (unexpected but long-running)
        return False


def cleanup_stale_pid(pid_file_path: str) -> None:
    """Remove stale PID file from a previous run.

    Args:
        pid_file_path: Path to the PID file
    """
    if os.path.exists(pid_file_path):
        try:
            os.remove(pid_file_path)
            logger.debug(f"Removed stale PID file: {pid_file_path}")
        except OSError:
            pass


def _signal_handler(sig, frame):
    """Handle SIGINT/SIGTERM for graceful shutdown."""
    global _subprocess
    logger.info(f"Received signal {sig}, shutting down...")
    if _subprocess and _subprocess.poll() is None:
        _subprocess.terminate()
        try:
            _subprocess.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _subprocess.kill()
    sys.exit(0)


def run_launcher(config: LauncherConfig) -> None:
    """Run the launcher with the given configuration.

    This is the main entry point for running a supervised STT process.

    Args:
        config: Launcher configuration

    :flow: STT Process Supervision
    :step: 1
    :description: Supervisor loop that manages the STT process lifecycle.
        Monitors for crashes, handles graceful restarts via flag file,
        and enforces crash thresholds to prevent infinite restart loops.
        The flag file mechanism allows WheelHouse to trigger clean restarts.
    :data_in: Flag file at RESTART_FLAG_PATH signals intentional restart
    :data_out: Spawns and monitors STT main.py subprocess
    """
    global _subprocess

    # Determine launcher directory
    launcher_dir = config.launcher_dir
    if launcher_dir is None:
        # Get the directory of the calling script
        import inspect
        frame = inspect.currentframe()
        if frame and frame.f_back:
            caller_file = frame.f_back.f_globals.get('__file__')
            if caller_file:
                launcher_dir = os.path.dirname(os.path.abspath(caller_file))
        if launcher_dir is None:
            launcher_dir = os.getcwd()

    # Compute paths
    pid_file_path = get_pid_file_path(config.app_name)
    restart_flag_path = get_restart_flag_path(config.app_name)
    main_script = os.path.join(launcher_dir, config.main_script)

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [%(levelname)s] {config.app_name}-Launcher - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Register signal handlers
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    cleanup_stale_pid(pid_file_path)

    crash_count = 0
    should_restart_process = True

    logger.info(f"{config.app_name} Launcher starting (supervisor for {main_script})")
    logger.info(f"Python executable: {sys.executable}")
    logger.info(f"Restart flag path: {restart_flag_path}")

    while should_restart_process and crash_count < config.max_crashes:
        start_time = time.time()

        try:
            # Start the main STT process
            # Build command with any forwarded arguments
            cmd = [sys.executable, main_script]
            if config.forward_args:
                cmd.extend(config.forward_args)
            logger.info(f"Starting {config.app_name} process: {' '.join(cmd)}")
            _subprocess = subprocess.Popen(
                cmd,
                cwd=launcher_dir
            )

            # Write PID file
            with open(pid_file_path, 'w') as f:
                f.write(str(_subprocess.pid))

            # Wait for process to exit
            _subprocess.wait()
            exit_code = _subprocess.returncode

            logger.info(f"{config.app_name} process exited with code {exit_code}")

        except Exception as e:
            logger.error(f"Failed to start/monitor {config.app_name} process: {e}")
            exit_code = 1

        finally:
            _subprocess = None
            # Clean up PID file
            if os.path.exists(pid_file_path):
                try:
                    os.remove(pid_file_path)
                except OSError:
                    pass

        uptime = time.time() - start_time

        # Check for restart flag first
        restart_flag_exists = os.path.exists(restart_flag_path)

        if restart_flag_exists:
            logger.info("Restart flag found - restarting...")
            try:
                os.remove(restart_flag_path)
                should_restart_process = True
                crash_count = 0  # Reset crash count on intentional restart
            except OSError as e:
                logger.error(f"Failed to remove restart flag: {e}")
                should_restart_process = False
        else:
            # Use the decision function
            should_restart_process = should_restart(
                exit_code=exit_code,
                uptime=uptime,
                restart_flag_exists=False,  # Already checked above
                crash_threshold_s=config.crash_threshold_s
            )

            if exit_code == 0:
                logger.info(f"{config.app_name} exited cleanly (code 0) after {uptime:.1f}s - not restarting")
            elif uptime < config.crash_threshold_s and should_restart_process:
                crash_count += 1
                logger.error(f"{config.app_name} crashed after {uptime:.1f}s. Crash count: {crash_count}/{config.max_crashes}")
            elif not should_restart_process:
                logger.info(f"{config.app_name} exited after {uptime:.1f}s with code {exit_code}")

    if crash_count >= config.max_crashes:
        logger.critical(f"{config.app_name} crashed too many times ({config.max_crashes}). Aborting.")

    logger.info(f"{config.app_name} Launcher exiting.")
