"""Test bootstrap for google_stt_server.

Ensures shared STT packages are importable in test environments where the
shared Poetry package is not installed into the active interpreter.
"""
import sys
from pathlib import Path

_service_dir = Path(__file__).resolve().parent.parent
_shared_dir = _service_dir.parent / "shared"

for _path in (_service_dir, _shared_dir):
    _s = str(_path)
    if _s not in sys.path:
        sys.path.insert(0, _s)
