# tests/test_speech_handler_user_patterns_path.py
"""Tests for SpeechHandler._resolve_user_patterns_file.

reviewer_0 (bulletproof.3.2): the live path resolves
``get_user_data_dir()/user_patterns.toml``. Under a frozen build
``get_user_data_dir()`` calls ``mkdir(parents=True)`` on the per-user app-data
root, which can raise (permission or disk error). An unguarded raise here would
crash speech initialization instead of degrading to system-only patterns. The
resolver must catch the failure and return an empty path, which the catalog and
manager both treat as "no user file".
"""
from types import SimpleNamespace

from speech.speech_handler import SpeechHandler


def _resolver(config=None):
    """Bind the real method to a minimal object exposing only .config."""
    fake = SimpleNamespace(config=config or {})
    return SpeechHandler._resolve_user_patterns_file.__get__(fake)


def test_resolve_honors_config_override():
    resolve = _resolver({"STT_USER_PATTERNS_FILE": "D:/custom/user_patterns.toml"})
    assert resolve() == "D:/custom/user_patterns.toml"


def test_resolve_degrades_to_empty_on_data_dir_failure(monkeypatch):
    import utils.system

    def boom(*_a, **_k):
        raise OSError("cannot create the app-data directory")

    monkeypatch.setattr(utils.system, "get_user_data_dir", boom)
    resolve = _resolver({})
    # Must degrade to "" (which the catalog treats as no user file), not raise.
    assert resolve() == ""
