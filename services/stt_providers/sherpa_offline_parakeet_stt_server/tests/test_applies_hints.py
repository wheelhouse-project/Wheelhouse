"""Parakeet declares whether it applies a saved hint (wh-boost-engine-qualification).

The value goes into the capabilities frame, and the Logic process refuses
the "boost" command when it is False, so the word is typed as dictation.

Ruling 2 of the boss (2026-09-21): the value is the engine's boosting
status when boosting was requested (a hints file was built), otherwise the
[hotwords] enabled flag. With boosting enabled and no hint saved yet, the
engine records "not requested", and a strict "active" reading would refuse
the first "boost", so the user could never add the first hint. The value
reads engine.hotwords_status, the same object _hotwords_notice reads.
"""
from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest

import main as parakeet_main
from sherpa_engine import HotwordsStatus


def _build_server(hotwords_enabled, status):
    """A ParakeetServer with the engine, forwarder and capture stubbed.

    Same shape as the fixture in test_ready_backend_name.py. The engine
    stub carries a real HotwordsStatus, so the value under test comes from
    the same object the ready notice reads.
    """
    capture = MagicMock()
    capture.get_stats = Mock(return_value={
        'captured': 0, 'drops': 0, 'qsize': 0, 'max_q': 0})
    with patch.object(parakeet_main, 'SherpaOfflineEngine') as engine_cls, \
         patch.object(parakeet_main, 'WSForwarder') as forwarder_cls, \
         patch.object(parakeet_main, 'get_audio_provider',
                      return_value=capture):
        engine_cls.return_value.hotwords_status = status
        server = parakeet_main.ParakeetServer(
            model_config={'model_path': 'C:/unused', 'use_gpu': False},
            engine_config={},
            hotwords_enabled=hotwords_enabled,
        )
    return server, forwarder_cls.return_value


def test_disabled_declares_no_hints():
    _server, forwarder = _build_server(False, HotwordsStatus())
    assert forwarder.applies_hints is False


def test_enabled_with_no_hint_yet_declares_hints():
    """No hints file yet: the engine records requested False. The first
    'boost' must still be accepted, or the user can never add a hint."""
    _server, forwarder = _build_server(True, HotwordsStatus())
    assert forwarder.applies_hints is True


def test_enabled_and_active_declares_hints():
    status = HotwordsStatus(requested=True, active=True)
    _server, forwarder = _build_server(True, status)
    assert forwarder.applies_hints is True


def test_enabled_and_rejected_declares_no_hints():
    """Boosting was requested but the engine refused it (for example a
    rejected bpe.vocab): the engine applies no hint."""
    status = HotwordsStatus(
        requested=True, active=False, detail="bpe.vocab unusable")
    _server, forwarder = _build_server(True, status)
    assert forwarder.applies_hints is False


@pytest.mark.parametrize(
    ("enabled", "status", "expected"),
    [
        (False, HotwordsStatus(), False),
        (True, HotwordsStatus(), True),
        (True, HotwordsStatus(requested=True, active=True), True),
        (True, HotwordsStatus(requested=True, active=False), False),
    ],
    ids=["disabled", "enabled-no-hint", "enabled-active", "enabled-rejected"],
)
def test_the_decision_function(enabled, status, expected):
    assert parakeet_main.applies_hints_value(status, enabled) is expected


def test_a_mock_status_cannot_claim_boosting():
    """A MagicMock status answers truthy for every attribute. The reads
    are `is True`, as in _hotwords_notice, so a mock falls back to the
    enabled flag instead of claiming 'requested'."""
    assert parakeet_main.applies_hints_value(MagicMock(), False) is False
    # With the flag on, a mock must fall back to True as well: a truthy
    # "requested" read would answer from the mock's "active" instead.
    assert parakeet_main.applies_hints_value(MagicMock(), True) is True
    assert parakeet_main.applies_hints_value(None, True) is True
