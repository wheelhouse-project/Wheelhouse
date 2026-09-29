"""Tests for shared_stt.mic_notice: the on-screen notices for a lost and a
recovered microphone (wh-mic-loss-notice).

The exact strings are asserted here because the boss ruled on their words
(D1, 00:47 2026-09-25) and a change to them is a user-visible change.
"""

from unittest.mock import Mock, call

from shared_stt.mic_notice import (
    MIC_LOST_KIND,
    MIC_LOST_MESSAGE,
    MIC_RECOVERED_KIND,
    MicOutageNotifier,
    recovery_message,
)


TITLE = "Parakeet v3 (GPU)"


def _notifier(forwarder):
    holder = {"forwarder": forwarder}
    return MicOutageNotifier(TITLE, lambda: holder["forwarder"]), holder


class TestTheNoticeWords:
    def test_the_loss_text_is_the_ruled_text(self):
        assert MIC_LOST_MESSAGE == (
            "Microphone lost. Speech recognition is waiting for it to "
            "come back.")

    def test_the_recovery_text_names_the_device(self):
        assert recovery_message("Microphone (4- Scarlett Solo USB)") == (
            "Microphone is back (Microphone (4- Scarlett Solo USB)). "
            "Speech recognition works again.")

    def test_the_recovery_text_without_a_device(self):
        expected = "Microphone is back. Speech recognition works again."
        assert recovery_message(None) == expected
        assert recovery_message("") == expected

    def test_the_kinds(self):
        assert MIC_LOST_KIND == "mic_lost"
        assert MIC_RECOVERED_KIND == "mic_recovered"


class TestTheNotifier:
    def test_a_loss_and_its_recovery_each_send_one_notice(self):
        forwarder = Mock()
        notifier, _ = _notifier(forwarder)

        notifier("lost", None)
        notifier("recovered", "USB Webcam Microphone")

        assert forwarder.send_notification.call_args_list == [
            call(TITLE, MIC_LOST_MESSAGE, kind="mic_lost"),
            call(TITLE,
                 "Microphone is back (USB Webcam Microphone). "
                 "Speech recognition works again.",
                 kind="mic_recovered"),
        ]

    def test_a_recovery_with_no_device_sends_the_plain_text(self):
        forwarder = Mock()
        notifier, _ = _notifier(forwarder)

        notifier("lost", None)
        notifier("recovered", None)

        assert forwarder.send_notification.call_args_list[-1] == call(
            TITLE, "Microphone is back. Speech recognition works again.",
            kind="mic_recovered")

    def test_no_forwarder_sends_nothing_and_does_not_raise(self):
        notifier, _ = _notifier(None)

        notifier("lost", None)
        notifier("recovered", "USB Webcam Microphone")

    def test_the_forwarder_is_read_at_call_time(self):
        """The provider builds its forwarder after the capture, so the
        notifier must not capture the value it had at construction."""
        notifier, holder = _notifier(None)
        forwarder = Mock()
        holder["forwarder"] = forwarder

        notifier("lost", None)

        forwarder.send_notification.assert_called_once_with(
            TITLE, MIC_LOST_MESSAGE, kind="mic_lost")

    def test_k2_a_loss_with_no_forwarder_sends_no_recovery(self):
        """Ruling K2: a lone "Microphone is back" would confuse. A loss
        seen before the forwarder existed sent nothing, so its recovery
        sends nothing either, even though a forwarder exists by then."""
        notifier, holder = _notifier(None)

        notifier("lost", None)
        forwarder = Mock()
        holder["forwarder"] = forwarder
        notifier("recovered", "USB Webcam Microphone")

        forwarder.send_notification.assert_not_called()

    def test_k2_the_next_outage_after_a_silent_one_is_announced(self):
        """The silent outage does not silence the ones after it."""
        notifier, holder = _notifier(None)
        notifier("lost", None)
        forwarder = Mock()
        holder["forwarder"] = forwarder
        notifier("recovered", None)

        notifier("lost", None)
        notifier("recovered", None)

        assert [c.kwargs["kind"] for c in
                forwarder.send_notification.call_args_list] == [
            "mic_lost", "mic_recovered"]

    def test_one_recovery_per_announced_loss(self):
        """A second recovery with no loss between them sends nothing."""
        forwarder = Mock()
        notifier, _ = _notifier(forwarder)

        notifier("lost", None)
        notifier("recovered", None)
        notifier("recovered", None)

        assert [c.kwargs["kind"] for c in
                forwarder.send_notification.call_args_list] == [
            "mic_lost", "mic_recovered"]

    def test_a_new_outage_whose_loss_was_not_sent_sends_no_recovery(self):
        """Ruling K2 across two outages. The first outage's loss notice
        was sent and it never recovered (known limit L1: stop() ends an
        outage silently). The next outage's loss send raises, so that
        outage sent no loss notice, and its recovery must stay silent
        rather than inherit the first outage's "sent"."""
        forwarder = Mock()
        notifier, _ = _notifier(forwarder)

        notifier("lost", None)
        forwarder.send_notification.side_effect = RuntimeError("closed")
        try:
            notifier("lost", None)
        except RuntimeError:
            pass
        forwarder.send_notification.side_effect = None
        forwarder.send_notification.reset_mock()
        notifier("recovered", None)

        forwarder.send_notification.assert_not_called()

    def test_a_forwarder_gone_by_the_recovery_sends_nothing(self):
        """The loss notice was sent, and the forwarder is gone when the
        microphone returns: the recovery sends nothing and does not
        raise."""
        forwarder = Mock()
        notifier, holder = _notifier(forwarder)

        notifier("lost", None)
        holder["forwarder"] = None
        notifier("recovered", "USB Webcam Microphone")

        forwarder.send_notification.assert_called_once_with(
            TITLE, MIC_LOST_MESSAGE, kind="mic_lost")

    def test_a_failed_loss_send_is_not_counted_as_handed_over(self):
        """The loss notice was not handed over when the send raised, so
        the recovery stays silent; the error reaches the caller, which is
        the capture's try/except (ruling K1)."""
        forwarder = Mock()
        forwarder.send_notification.side_effect = RuntimeError("closed")
        notifier, _ = _notifier(forwarder)

        try:
            notifier("lost", None)
        except RuntimeError:
            pass
        forwarder.send_notification.side_effect = None
        forwarder.send_notification.reset_mock()
        notifier("recovered", None)

        forwarder.send_notification.assert_not_called()

    def test_no_notice_carries_anything_but_the_ruled_words(self):
        """Acceptance 4: no audio data and no recognized text; only the
        title, the ruled text, and the kind are passed."""
        forwarder = Mock()
        notifier, _ = _notifier(forwarder)

        notifier("lost", None)
        notifier("recovered", "Mic")

        for c in forwarder.send_notification.call_args_list:
            assert len(c.args) == 2
            assert set(c.kwargs) == {"kind"}
