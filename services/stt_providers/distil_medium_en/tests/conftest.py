"""Test bootstrap for the distil-medium-en STT provider.

Puts the service directory on sys.path so tests can import the service
modules (e.g. `import main`) without installing the package (it runs in
uv's `package = false`). Mirrors the parakeet service's conftest.
"""
from __future__ import annotations

import sys
from pathlib import Path

_service_dir = Path(__file__).resolve().parent.parent
if str(_service_dir) not in sys.path:
    sys.path.insert(0, str(_service_dir))
