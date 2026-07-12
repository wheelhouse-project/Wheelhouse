"""Supervised launcher for distil-whisper medium.en STT provider (GPU)."""
import sys
from shared_stt.launcher import run_launcher, LauncherConfig

if __name__ == "__main__":
    forward_args = sys.argv[1:]
    config = LauncherConfig(
        app_name="distil_medium_en",
        main_script="main.py",
        forward_args=forward_args if forward_args else None,
    )
    run_launcher(config)
