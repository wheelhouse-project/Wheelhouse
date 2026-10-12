"""On-screen notices for a lost and a recovered microphone (wh-mic-loss-notice).

The WinRT capture rebuilds its AudioGraph by itself when the microphone
disappears (wh-mic-loss-capture-recovery), but its loss and recovery lines
are WARNING and INFO, and the Windows error notification shows ERROR lines
only. So the user could not tell a lost microphone from speech recognition
that is not hearing them. The capture now calls an outage callback, and
this module is that callback for all three providers, so the three share
one text and one rule.

The words below were ruled by the boss (D1, 00:47 2026-09-25) and are a
user-visible contract: tests assert them exactly. No notice carries audio
data or recognized text; the recovery notice may carry the device name
(acceptance 4).
"""

import logging
from typing import Callable, Optional, Protocol

logger = logging.getLogger(__name__)

# The kind values WheelHouse routes on. Both are exempt from its startup
# suppression (integrations/websocket_manager.py,
# _STARTUP_SUPPRESSION_EXEMPT_KINDS): a microphone lost during a launch is
# a real reason speech fails.
MIC_LOST_KIND = "mic_lost"
MIC_RECOVERED_KIND = "mic_recovered"

MIC_LOST_MESSAGE = (
    "Microphone lost. Speech recognition is waiting for it to come back.")
MIC_RECOVERED_MESSAGE = "Microphone is back. Speech recognition works again."
MIC_RECOVERED_ON_DEVICE_MESSAGE = (
    "Microphone is back ({device}). Speech recognition works again.")

# The event names the capture passes as the callback's first argument
# (shared_audio/capture/winrt_capture.py, WinRTAudioCapture._report_outage).
# "lost" and "recovered" also drive the two notices; "opened" and "closed"
# carry only the microphone name (wh-show-microphone-name).
OUTAGE_LOST = "lost"
OUTAGE_RECOVERED = "recovered"
OUTAGE_OPENED = "opened"
OUTAGE_CLOSED = "closed"


class _NoticeForwarder(Protocol):
    """The forwarder methods this module calls (shared_stt.ws_forwarder
    .WSForwarder.send_notification and .send_microphone)."""

    def send_notification(self, title: str, message: str,
                          kind: str = "") -> None: ...

    def send_microphone(self, device: str) -> None: ...


def recovery_message(device: Optional[str]) -> str:
    """The recovery text, naming the device when the capture knows it.

    The rebuild opens Windows's CURRENT default microphone, which may not
    be the one that was lost, so the name tells the user which one works.
    """
    if device:
        return MIC_RECOVERED_ON_DEVICE_MESSAGE.format(device=device)
    return MIC_RECOVERED_MESSAGE


class MicOutageNotifier:
    """The capture's outage_callback: sends the loss and recovery notices
    and reports the open microphone's name.

    Called on the capture thread as notifier(event, device), with event
    OUTAGE_LOST, OUTAGE_RECOVERED, OUTAGE_OPENED or OUTAGE_CLOSED. The
    forwarder gets the name on all four events (OUTAGE_OPENED and
    OUTAGE_RECOVERED carry it; the other two send "", meaning none is
    open), and the name is never gated by the loss flag that gates the two
    notices (wh-show-microphone-name). The capture calls it inside its own
    try/except (ruling K1), so an exception here costs one notice and never
    the recovery.

    Args:
        title: The provider's display title, the same one its ready and
            startup notices carry.
        get_forwarder: Returns the provider's forwarder, or None. Read at
            each call rather than once, because every provider builds its
            forwarder after its capture.
    """

    def __init__(self, title: str,
                 get_forwarder: Callable[[], Optional[_NoticeForwarder]]):
        self._title = title
        self._get_forwarder = get_forwarder
        # Whether the current outage's loss notice was handed to a
        # forwarder. Ruling K2: the recovery notice goes out only then,
        # because a lone "Microphone is back" would confuse. Written only
        # by the capture thread, the one thread that calls this.
        self._loss_announced = False

    def _report_microphone(self, name: str) -> None:
        """Tell the forwarder which microphone is open ("" for none).

        Deliberately NOT gated by _loss_announced: the name WheelHouse shows
        must be right whether or not a notice went out
        (wh-show-microphone-name). A fault here is logged and swallowed so
        it can never cost the notice that follows it.
        """
        forwarder = self._get_forwarder()
        if forwarder is None:
            return
        try:
            forwarder.send_microphone(name)
        except Exception as e:
            logger.warning(
                f'The microphone name report failed: '
                f'{type(e).__name__}: {e}')

    def __call__(self, event: str, device: Optional[str]) -> None:
        if event == OUTAGE_OPENED:
            self._report_microphone(device or "")
        elif event == OUTAGE_CLOSED:
            self._report_microphone("")
        elif event == OUTAGE_LOST:
            self._report_microphone("")
            self._loss_announced = False
            forwarder = self._get_forwarder()
            if forwarder is None:
                return
            # crewcut: no rate limit. A microphone that drops out
            # repeatedly (samples between each loss) gives two notices per
            # dropout (ruling K3, known limit L3). To remove: keep the
            # time of the last loss notice and skip a loss that comes
            # within a minimum gap of it, together with its recovery.
            forwarder.send_notification(
                self._title, MIC_LOST_MESSAGE, kind=MIC_LOST_KIND)
            self._loss_announced = True
        elif event == OUTAGE_RECOVERED:
            self._report_microphone(device or "")
            if not self._loss_announced:
                return
            self._loss_announced = False
            forwarder = self._get_forwarder()
            if forwarder is None:
                return
            forwarder.send_notification(
                self._title, recovery_message(device),
                kind=MIC_RECOVERED_KIND)
        else:
            logger.debug(f'Unknown microphone outage event {event!r}')
