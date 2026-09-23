"""The Parakeet server arms the wake word for the reasons the shared rule names.

wh-wake-word-stop-listening (boss ruling R2): idle_recovery arms on every
disable reason WheelHouse sends except "ptt" -- "idle", "manual",
"startup", "audio" and "sonos" -- so the wake word turns listening on
whatever switched it off. push_to_talk keeps its own list ("idle",
"audio", "sonos"), unchanged.

These tests drive the real `_handle_wake_word_activate`, which is where the
arming decision lands. The rule it applies lives in one shared function,
`shared_stt.wake_word_detector.should_listen_for_wake_word`; the shared
suite covers the rule itself (services/stt_providers/shared/tests/
test_wake_word_detector.py) and these tests cover this provider's use of it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from main import ParakeetServer


def _server(mode: str, is_loaded: bool = True) -> ParakeetServer:
    """A ParakeetServer shell carrying only what the handler reads.

    The same __new__ shell this service's other test files use
    (tests/test_hotwords.py): the real constructor opens a microphone and
    loads the Parakeet model.
    """
    server = ParakeetServer.__new__(ParakeetServer)
    server._wake_word_mode = mode
    server._wake_word_listening = False
    detector = MagicMock()
    detector.is_loaded = is_loaded
    server._wake_word_detector = detector
    return server


class TestWakeWordArming:
    @pytest.mark.parametrize(
        "reason", ["idle", "manual", "startup", "audio", "sonos"]
    )
    def test_every_pause_reason_arms_in_idle_recovery(self, reason):
        """wh-wake-word-stop-listening R2: the wake word ends any pause."""
        server = _server("idle_recovery")
        server._handle_wake_word_activate(reason)
        assert server._wake_word_listening is True
        server._wake_word_detector.reset.assert_called_once()

    def test_ptt_reason_does_not_arm_in_idle_recovery(self):
        """A push-to-talk release is not a pause the wake word ends."""
        server = _server("idle_recovery")
        server._handle_wake_word_activate("ptt")
        assert server._wake_word_listening is False
        server._wake_word_detector.reset.assert_not_called()

    def test_manual_reason_does_not_arm_in_push_to_talk(self):
        """push_to_talk keeps its own list, which names no "manual"."""
        server = _server("push_to_talk")
        server._handle_wake_word_activate("manual")
        assert server._wake_word_listening is False
        server._wake_word_detector.reset.assert_not_called()

    def test_sonos_reason_arms_in_push_to_talk(self):
        """push_to_talk arms on every disable reason, unchanged."""
        server = _server("push_to_talk")
        server._handle_wake_word_activate("sonos")
        assert server._wake_word_listening is True
        server._wake_word_detector.reset.assert_called_once()

    def test_no_reason_deactivates(self):
        """reason None means transcription was re-enabled: stop listening."""
        server = _server("idle_recovery")
        server._handle_wake_word_activate("idle")
        assert server._wake_word_listening is True
        server._handle_wake_word_activate(None)
        assert server._wake_word_listening is False

    def test_unloaded_detector_never_arms(self):
        """A detector whose model never loaded cannot hear the wake word."""
        server = _server("idle_recovery", is_loaded=False)
        server._handle_wake_word_activate("idle")
        assert server._wake_word_listening is False
        server._wake_word_detector.reset.assert_not_called()
