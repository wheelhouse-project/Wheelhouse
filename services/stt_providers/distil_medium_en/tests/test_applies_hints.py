"""Distil-Whisper declares whether it applies a saved hint
(wh-boost-engine-qualification).

The value goes into the capabilities frame, and the Logic process refuses
the "boost" command when it is False, so the word is typed as dictation.
Ruling 2 of the boss (2026-09-21): Distil-Whisper reports its startup
[hotwords] enabled flag, not whether a hotwords string was built, so the
first "boost" with boosting enabled and no hint saved is still accepted.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from main import DistilMediumServer


def _forwarder_after_build(**kwargs):
    with (
        patch("main.get_audio_provider"),
        patch("main.WhisperStreamingEngine"),
        patch("main.WSForwarder") as forwarder_cls,
        patch("main.AudioProcessor"),
    ):
        DistilMediumServer(model_config={}, engine_config={}, **kwargs)
    return forwarder_cls.return_value


@pytest.mark.parametrize("enabled", [True, False])
def test_declares_its_startup_hotwords_flag(enabled):
    forwarder = _forwarder_after_build(hotwords_enabled=enabled)
    assert forwarder.applies_hints is enabled


def test_enabled_with_no_hint_yet_still_declares_hints():
    """No hints saved yet: no hotwords string. The flag still decides."""
    forwarder = _forwarder_after_build(hotwords=None, hotwords_enabled=True)
    assert forwarder.applies_hints is True


def test_default_construction_declares_no_hints():
    """[hotwords] is off by default (wh-distil-hotwords-decode-collapse)."""
    forwarder = _forwarder_after_build()
    assert forwarder.applies_hints is False
