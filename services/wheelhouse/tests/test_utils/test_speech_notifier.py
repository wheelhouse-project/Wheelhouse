"""Tests for speech_notifier.py - Speech state toast notifications.

Tests cover:
- SpeechNotifier enable/disable behavior
- Notification methods (disabled, enabled, suppression change, debug)
- Details appended when provided
- Notification failure resilience
"""

from unittest.mock import Mock, patch

import pytest


class TestSpeechNotifier:
    """Tests for the SpeechNotifier class."""

    @pytest.fixture
    def mock_notification(self):
        with patch("utils.speech_notifier.notification") as m:
            yield m

    def test_notify_speech_disabled(self, mock_notification):
        from utils.speech_notifier import SpeechNotifier

        notifier = SpeechNotifier(enabled=True)
        notifier.notify_speech_disabled("audio playing")

        mock_notification.notify.assert_called_once()
        kwargs = mock_notification.notify.call_args[1]
        assert "Disabled" in kwargs["title"]
        assert "audio playing" in kwargs["message"]

    def test_notify_speech_disabled_with_details(self, mock_notification):
        from utils.speech_notifier import SpeechNotifier

        notifier = SpeechNotifier(enabled=True)
        notifier.notify_speech_disabled("audio", details="Spotify playing")

        kwargs = mock_notification.notify.call_args[1]
        assert "Spotify playing" in kwargs["message"]

    def test_notify_speech_enabled(self, mock_notification):
        from utils.speech_notifier import SpeechNotifier

        notifier = SpeechNotifier(enabled=True)
        notifier.notify_speech_enabled("audio stopped")

        kwargs = mock_notification.notify.call_args[1]
        assert "Enabled" in kwargs["title"]
        assert "audio stopped" in kwargs["message"]

    def test_notify_suppression_suppressed(self, mock_notification):
        from utils.speech_notifier import SpeechNotifier

        notifier = SpeechNotifier(enabled=True)
        notifier.notify_suppression_change("audio", is_suppressed=True)

        kwargs = mock_notification.notify.call_args[1]
        assert "Suppressed" in kwargs["title"]
        assert "audio" in kwargs["message"]

    def test_notify_suppression_unsuppressed(self, mock_notification):
        from utils.speech_notifier import SpeechNotifier

        notifier = SpeechNotifier(enabled=True)
        notifier.notify_suppression_change("sonos", is_suppressed=False)

        kwargs = mock_notification.notify.call_args[1]
        assert "Un-suppressed" in kwargs["title"]

    def test_notify_debug(self, mock_notification):
        from utils.speech_notifier import SpeechNotifier

        notifier = SpeechNotifier(enabled=True)
        notifier.notify_debug("test message")

        kwargs = mock_notification.notify.call_args[1]
        assert "Debug" in kwargs["title"]
        assert "test message" in kwargs["message"]

    def test_disabled_skips_all_notifications(self, mock_notification):
        from utils.speech_notifier import SpeechNotifier

        notifier = SpeechNotifier(enabled=False)
        notifier.notify_speech_disabled("reason")
        notifier.notify_speech_enabled("reason")
        notifier.notify_suppression_change("type", True)
        notifier.notify_debug("msg")

        mock_notification.notify.assert_not_called()

    def test_set_enabled_toggles(self, mock_notification):
        from utils.speech_notifier import SpeechNotifier

        notifier = SpeechNotifier(enabled=True)
        notifier.set_enabled(False)
        assert notifier.enabled is False

        notifier.set_enabled(True)
        assert notifier.enabled is True

    def test_notification_failure_doesnt_raise(self, mock_notification):
        from utils.speech_notifier import SpeechNotifier

        mock_notification.notify.side_effect = RuntimeError("broken")
        notifier = SpeechNotifier(enabled=True)

        # Should not raise
        notifier.notify_speech_disabled("test")

    def test_notify_not_callable_handled(self, mock_notification):
        from utils.speech_notifier import SpeechNotifier

        mock_notification.notify = "not callable"
        notifier = SpeechNotifier(enabled=True)

        # Should not raise
        notifier.notify_speech_disabled("test")

    # wh-mgbik.1: dictation-drop notification for the focus-redirect
    # _notify_user hook. wh-mgbik.1.1.1 / wh-mgbik.1.1.2 (codex
    # findings): bypasses the enabled flag and routes through the
    # NotifierWorker.

    def test_notify_dictation_drop_uses_notifier_worker(self, mock_notification):
        """When a NotifierWorker is registered, dictation-drop submits
        a NotifierPayload to the worker (not the inline plyer call)."""
        from unittest.mock import MagicMock

        from utils.speech_notifier import SpeechNotifier

        worker = MagicMock()
        worker.submit.return_value = True

        with patch("utils.logging_setup.get_notifier_worker", return_value=worker):
            notifier = SpeechNotifier(enabled=True)
            notifier.notify_dictation_drop("terminal busy (dropped 2 word(s))")

        worker.submit.assert_called_once()
        payload = worker.submit.call_args[0][0]
        assert "Dictation" in payload.title
        assert "terminal busy" in payload.message
        assert "2 word(s)" in payload.message
        # The inline plyer call must NOT fire when the worker is wired.
        mock_notification.notify.assert_not_called()

    def test_notify_dictation_drop_bypasses_enabled_flag(self, mock_notification):
        """wh-mgbik.1.1.1: accessibility-critical notification fires
        even when the SpeechNotifier instance is constructed disabled
        (which is the production default at state_manager.py:90)."""
        from unittest.mock import MagicMock

        from utils.speech_notifier import SpeechNotifier

        worker = MagicMock()
        worker.submit.return_value = True

        with patch("utils.logging_setup.get_notifier_worker", return_value=worker):
            notifier = SpeechNotifier(enabled=False)
            notifier.notify_dictation_drop("anything")

        worker.submit.assert_called_once()

    def test_notify_dictation_drop_falls_back_inline_when_no_worker(
        self, mock_notification,
    ):
        """When no NotifierWorker is registered (test or early-init),
        the method falls back to the inline _send_notification path so
        the notification still reaches the user."""
        from utils.speech_notifier import SpeechNotifier

        with patch("utils.logging_setup.get_notifier_worker", return_value=None):
            notifier = SpeechNotifier(enabled=True)
            notifier.notify_dictation_drop("fallback path")

        mock_notification.notify.assert_called_once()
        kwargs = mock_notification.notify.call_args[1]
        assert "Dictation" in kwargs["title"]
        assert "fallback path" in kwargs["message"]

    def test_notify_dictation_drop_falls_back_inline_when_worker_queue_full(
        self, mock_notification, caplog,
    ):
        """wh-mgbik.1.2.1 (deepseek): when the NotifierWorker.submit
        returns False (bounded queue is full), the method falls back to
        the inline plyer call so the accessibility-critical notification
        still reaches the user. A WARNING log entry records the
        overflow."""
        import logging
        from unittest.mock import MagicMock

        from utils.speech_notifier import SpeechNotifier

        worker = MagicMock()
        worker.submit.return_value = False

        with caplog.at_level(logging.WARNING, logger="utils.speech_notifier"):
            with patch(
                "utils.logging_setup.get_notifier_worker", return_value=worker,
            ):
                notifier = SpeechNotifier(enabled=True)
                notifier.notify_dictation_drop("queue full message")

        worker.submit.assert_called_once()
        mock_notification.notify.assert_called_once()
        kwargs = mock_notification.notify.call_args[1]
        assert "Dictation" in kwargs["title"]
        assert "queue full message" in kwargs["message"]
        assert any(
            "worker queue full" in record.getMessage()
            for record in caplog.records
        )

    def test_notify_dictation_drop_uses_neutral_title(
        self, mock_notification,
    ):
        """wh-mgbik.1.2.5 (deepseek): the toast title is neutral
        ("WheelHouse: Dictation") so the rendered toast does not say
        "rejected" above a body that carries a different failure verb
        ("timed out", "failed", etc.)."""
        from unittest.mock import MagicMock

        from utils.speech_notifier import SpeechNotifier

        worker = MagicMock()
        worker.submit.return_value = True

        with patch(
            "utils.logging_setup.get_notifier_worker", return_value=worker,
        ):
            notifier = SpeechNotifier(enabled=True)
            notifier.notify_dictation_drop("dictation submit timed out")

        payload = worker.submit.call_args[0][0]
        assert payload.title == "WheelHouse: Dictation"
