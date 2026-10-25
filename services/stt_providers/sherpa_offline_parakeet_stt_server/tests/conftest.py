"""Test bootstrap for the parakeet STT provider.

Puts the service directory on sys.path so tests can import the service
modules (e.g. `from sherpa_engine import SherpaOfflineEngine`) without
installing the package (it runs in uv's `package = false`).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_service_dir = Path(__file__).resolve().parent.parent
if str(_service_dir) not in sys.path:
    sys.path.insert(0, str(_service_dir))


@pytest.fixture(autouse=True)
def no_engine_audio_saving_from_the_shell(monkeypatch):
    """A developer shell that sets WHEELHOUSE_SAVE_ENGINE_AUDIO must not make
    a test write or delete files in the real %LOCALAPPDATA%. A test that needs
    the variable sets it itself with monkeypatch.setenv
    (wh-first-sound-lost.1)."""
    monkeypatch.delenv("WHEELHOUSE_SAVE_ENGINE_AUDIO", raising=False)
