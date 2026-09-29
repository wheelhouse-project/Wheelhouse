"""Parakeet puts a notice on the screen when the microphone is lost and
when it comes back (wh-mic-loss-notice).

The capture sees the loss; the provider owns the forwarder. So the
provider hands the capture an outage_callback at its main
get_audio_provider call, and that callback sends through self.forwarder
with the provider's display title, the same title its ready notice
carries. The forwarder is built after the capture, so the callback reads
it at call time.
"""
from __future__ import annotations

from unittest.mock import MagicMock, Mock, call, patch

import pytest

import main as parakeet_main
from shared_stt.mic_notice import MIC_LOST_MESSAGE


@pytest.fixture
def built():
    """A ParakeetServer with the engine, forwarder and capture stubbed,
    plus the factory mock that recorded how the capture was asked for."""
    with patch.object(parakeet_main, 'SherpaOfflineEngine'), \
         patch.object(parakeet_main, 'WSForwarder'), \
         patch.object(parakeet_main, 'get_audio_provider',
                      return_value=MagicMock()) as factory:
        server = parakeet_main.ParakeetServer(
            model_config={'model_path': 'C:/unused', 'use_gpu': False},
            engine_config={},
        )
    return server, factory


def _outage_callback(factory):
    factory.assert_called_once()
    callback = factory.call_args.kwargs.get('outage_callback')
    assert callable(callback), (
        'the main get_audio_provider call passed no outage_callback')
    return callback


def test_the_capture_is_given_an_outage_callback(built):
    _server, factory = built
    _outage_callback(factory)


def test_the_callback_sends_both_notices_with_the_display_title(built):
    server, factory = built
    callback = _outage_callback(factory)
    # Replaced after construction on purpose: the callback must read the
    # forwarder when it runs, not the one that existed when it was made.
    server.forwarder = Mock()

    callback('lost', None)
    callback('recovered', 'Microphone (4- Scarlett Solo USB)')

    assert server.forwarder.send_notification.call_args_list == [
        call(server.display_name, MIC_LOST_MESSAGE, kind='mic_lost'),
        call(server.display_name,
             'Microphone is back (Microphone (4- Scarlett Solo USB)). '
             'Speech recognition works again.',
             kind='mic_recovered'),
    ]


def test_no_forwarder_sends_nothing(built):
    server, factory = built
    callback = _outage_callback(factory)
    server.forwarder = None

    callback('lost', None)
    callback('recovered', None)


def test_list_devices_passes_no_outage_callback():
    """A --list-devices run has no forwarder and shows no notices."""
    mic = MagicMock()
    mic.list_audio_devices.return_value = []
    with patch.object(parakeet_main, 'get_audio_provider',
                      return_value=mic) as factory:
        assert parakeet_main.run_list_devices() == 0

    assert 'outage_callback' not in factory.call_args.kwargs
