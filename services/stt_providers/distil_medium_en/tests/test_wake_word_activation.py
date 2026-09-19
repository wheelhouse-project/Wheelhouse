"""The distil server arms the wake word for the reasons the shared rule names.

wh-audio-suppression-control C3. "audio" armed idle_recovery for one
release, so the user could say the command that switched audio
suppression off while the sound that caused the pause kept playing.
wh-audio-suppression-auto removed that command and the recovery window
it was spoken into, so idle_recovery now arms on "idle" alone and a
sound pause ends when the sound stops. push_to_talk still arms on
every reason, because its hold mutes the speakers.

These tests drive the real `_handle_wake_word_activate`, which is where the
arming decision lands. The rule it applies lives in one shared function,
`shared_stt.wake_word_detector.should_listen_for_wake_word`; the shared
suite covers the rule itself (services/stt_providers/shared/tests/
test_wake_word_detector.py) and these tests cover this provider's use of it.
The parakeet service's tests/test_wake_word_activation.py is the template.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from main import DistilMediumServer


def _server(mode: str, is_loaded: bool = True) -> DistilMediumServer:
    """A DistilMediumServer shell carrying only what the handler reads.

    The same __new__ shell this service's other test files use
    (tests/test_hint_handlers.py): the real constructor opens a microphone
    and loads the Whisper model.
    """
    server = DistilMediumServer.__new__(DistilMediumServer)
    server._wake_word_mode = mode
    server._wake_word_listening = False
    detector = MagicMock()
    detector.is_loaded = is_loaded
    server._wake_word_detector = detector
    return server


class TestWakeWordArming:
    def test_audio_reason_does_not_arm_in_idle_recovery(self):
        """wh-audio-suppression-auto: the sound pause has no voice way out.

        The detector armed on "audio" so the user could say the command
        that switched audio suppression off. That command is gone, and so
        is the recovery window it was spoken into.
        """
        server = _server("idle_recovery")
        server._handle_wake_word_activate("audio")
        assert server._wake_word_listening is False
        server._wake_word_detector.reset.assert_not_called()

    def test_sonos_reason_does_not_arm_in_idle_recovery(self):
        """The Sonos pause keeps the behaviour it has today in this mode."""
        server = _server("idle_recovery")
        server._handle_wake_word_activate("sonos")
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
