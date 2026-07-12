"""System-level utility functions."""
import os
import sys
import tempfile
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

def get_app_data_path() -> str:
    """
    Returns a reliable, user-specific, writable directory for app data.
    Creates the directory if it doesn't exist.
    """
    app_data_path = os.getenv('APPDATA')
    if app_data_path:
        path = os.path.join(app_data_path, 'WheelHouse')
    else:
        # Fallback for non-Windows or if APPDATA is not set
        home = os.path.expanduser("~")
        path = os.path.join(home, '.wheelhouse')

    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.error(f"Could not create app data directory at {path}: {e}")
        # Fallback to a temporary directory if user-specific path fails
        path = os.path.join(tempfile.gettempdir(), 'WheelHouse')
        os.makedirs(path, exist_ok=True)
        logger.warning(f"Using temporary directory for app data: {path}")
    return path


def get_user_data_dir() -> Path:
    """Return the directory for user-owned state files (wh-k8ef).

    Under a PyInstaller frozen build, ``__file__`` resolves inside the
    temporary ``_MEIxxxxxx`` extraction directory that is wiped on
    exit, so user state (soft-allow grants, declines, pending
    counters) must live under the persistent app-data root instead.
    In a source checkout this is ``services/wheelhouse/data`` so the
    developer iteration loop is unchanged.

    Every call site that reads or writes user state must resolve
    through this helper; Logic and Input run in separate processes and
    only agree on paths because both derive them here.
    """
    if getattr(sys, "frozen", False):
        data_dir = Path(get_app_data_path()) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    return Path(__file__).resolve().parents[1] / "data"


def get_bundled_data_dir() -> Path:
    """Return the directory for read-only data shipped with the app.

    Frozen builds read shipped data (the soft-allow starter list) from
    inside the bundle (``sys._MEIPASS``), never from the per-user
    app-data root -- a user grant must not be able to clobber shipped
    entries. In a source checkout shipped data and user state share
    ``services/wheelhouse/data``.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "data"
    return Path(__file__).resolve().parents[1] / "data"
