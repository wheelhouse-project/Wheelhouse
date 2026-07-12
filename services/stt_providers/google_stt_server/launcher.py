"""Supervised launcher for Google STT server.

This launcher supervises the STT process, handles crash recovery,
and provides restart capability via flag file.

Typical Usage:
  python launcher.py --ws-host localhost --ws-port <port>
"""
import sys
from shared_stt.launcher import run_launcher, LauncherConfig

if __name__ == "__main__":
    # Forward all CLI args to main.py (e.g., --ws-host, --ws-port)
    forward_args = sys.argv[1:]

    config = LauncherConfig(
        app_name="google_stt",  # Must match [provider] name in config.toml
        main_script="main.py",
        forward_args=forward_args if forward_args else None,
    )
    run_launcher(config)
