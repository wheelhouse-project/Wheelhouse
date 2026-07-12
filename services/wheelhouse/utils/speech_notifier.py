"""Speech state notification utility for WheelHouse.

This module provides Windows toast notifications for speech recognition state changes,
helping debug and monitor speech suppression events. It integrates with the existing
plyer notification system to provide immediate feedback about speech state changes.
"""

import logging
from typing import Optional
from plyer import notification

from utils.redact import redact_transcript

logger = logging.getLogger(__name__)

class SpeechNotifier:
    """Handles notifications for speech state changes."""
    
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        
    def notify_speech_disabled(self, reason: str, details: Optional[str] = None):
        """Send notification when speech is disabled."""
        if not self.enabled:
            return
            
        title = "WheelHouse: Speech Disabled"
        message = f"Reason: {reason}"
        if details:
            message += f"\n{details}"
            
        self._send_notification(title, message)
        
    def notify_speech_enabled(self, reason: str, details: Optional[str] = None):
        """Send notification when speech is enabled."""
        if not self.enabled:
            return
            
        title = "WheelHouse: Speech Enabled"
        message = f"Reason: {reason}"
        if details:
            message += f"\n{details}"
            
        self._send_notification(title, message)
        
    def notify_suppression_change(self, suppression_type: str, is_suppressed: bool, details: Optional[str] = None):
        """Send notification for suppression state changes."""
        if not self.enabled:
            return
            
        action = "Suppressed" if is_suppressed else "Un-suppressed"
        title = f"WheelHouse: Speech {action}"
        message = f"Cause: {suppression_type}"
        if details:
            message += f"\n{details}"
            
        self._send_notification(title, message)
        
    def _send_notification(self, title: str, message: str):
        """Internal method to send notification."""
        try:
            if hasattr(notification, 'notify') and callable(notification.notify):
                notification.notify(
                    title=title,
                    message=message,
                    app_name='WheelHouse',
                    timeout=3
                )
                logger.debug(
                    f"Notification sent: {title} - "
                    f"{redact_transcript(message)}"
                )
            else:
                logger.warning("Plyer notification.notify not available")
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")
            
    def set_enabled(self, enabled: bool):
        """Enable or disable notifications."""
        self.enabled = enabled
        logger.info(f"Speech notifications {'enabled' if enabled else 'disabled'}")
        
    def notify_debug(self, message: str):
        """Send debug notification for testing."""
        if not self.enabled:
            return
        self._send_notification("WheelHouse: Debug", message)

    def notify_dictation_drop(self, message: str):
        """Send notification when the focus-redirect path drops dictation.

        wh-mgbik.1: the focus-redirect ``_notify_user`` hook routes
        fail-closed user feedback (terminal_busy, editor_already_open,
        submit timeout, focus loss, drained-word IPC failure) through
        this method so the user gets a Windows notification instead of
        a silent log entry.

        Three policy differences from the other ``notify_*`` methods on
        this class (wh-mgbik.1.1.1 / wh-mgbik.1.1.2 codex findings and
        wh-mgbik.1.2.1 / wh-mgbik.1.2.5 deepseek findings):

          1. The ``enabled`` flag is NOT consulted. Dictation rejection
             feedback is accessibility-critical: a hands-free user who
             cannot see the editor open or close needs to learn that
             their words were dropped, regardless of the debug/
             suppression toggle that gates the other notifications.
          2. Delivery routes through the global ``NotifierWorker`` so
             the slow plyer call does not block the speech processor's
             asyncio loop. The worker owns its own daemon thread and
             a bounded queue (drop on overflow). If no worker is
             available (test or early-init context), or if the worker
             queue is full, the method falls back to the inline
             ``_send_notification`` path so the user still gets the
             notification. A worker-queue-full fallback logs at
             WARNING so the overflow is visible in the audit trail.
          3. The title is neutral ("WheelHouse: Dictation") so the
             rendered toast does not say "rejected" above a body that
             says "timed out" or "failed". The message body carries
             the specific failure verb.
        """
        title = "WheelHouse: Dictation"
        try:
            from utils.logging_setup import get_notifier_worker
            from utils.notifier_worker import NotifierPayload

            worker = get_notifier_worker()
            if worker is not None:
                payload = NotifierPayload(
                    title=title,
                    message=message,
                    levelname="INFO",
                    trace_id="",
                )
                if worker.submit(payload):
                    return
                logger.warning(
                    "notify_dictation_drop: worker queue full -- "
                    "falling back to inline plyer for message: %r",
                    message,
                )
        except Exception:
            pass
        self._send_notification(title, message)