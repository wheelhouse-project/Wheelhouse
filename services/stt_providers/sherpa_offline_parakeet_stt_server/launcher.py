"""Supervised launcher for Parakeet TDT STT provider."""

# Wheelhouse: put the owned Microsoft Visual C++ runtime folder on this
# process's library search path BEFORE any extension module loads. The order
# is the whole fix -- os.add_dll_directory cannot displace a library the
# process already holds. services/runtime_dll_directory.py explains it.
import os.path
import sys

_services_dir = os.path.abspath(__file__)
while (os.path.basename(_services_dir) != "services"
       and os.path.dirname(_services_dir) != _services_dir):
    _services_dir = os.path.dirname(_services_dir)
if _services_dir not in sys.path:
    sys.path.append(_services_dir)
from runtime_dll_directory import add_runtime_dll_directory

add_runtime_dll_directory()

import sys
from shared_stt.launcher import run_launcher, LauncherConfig

if __name__ == "__main__":
    forward_args = sys.argv[1:]
    config = LauncherConfig(
        app_name="parakeet_tdt",
        main_script="main.py",
        forward_args=forward_args if forward_args else None,
    )
    run_launcher(config)
