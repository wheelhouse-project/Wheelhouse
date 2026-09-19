"""Audio provider factory for STT services.

One capture path, no selection. David ruled on 2026-09-05: "The rule is we
always use WinRT." get_audio_provider() returns a WinRTAudioCapture, or it
raises. There is no backend argument, no availability list, and no
fallback to PortAudio through sounddevice (wh-capture-winrt-required).

WHY THE RULE EXISTS
-------------------
The factory used to prefer WinRT and fall back to sounddevice without
telling anyone. On 2026-09-05 the Parakeet venv had no winsdk, so the
provider that started at 15:25 captured through PortAudio for its whole
run and nothing said so. Distil-Whisper could never take the WinRT path at
all, because winsdk was not among its dependencies. A silent second path
means two capture behaviours in the field and one of them untested.

WHY THE FACTORY STAYS AT ALL
----------------------------
All three providers already call it, and the OVERFLOW_SOURCE contract
described below lives here. Constructing WinRTAudioCapture in each
provider would copy that contract three times.

WHAT A CALLER MUST DO WITH THE RAISE
------------------------------------
Catch RuntimeError, send the startup-failed notice carrying str(exc), and
exit nonzero. Do not start without capture: a provider that runs without a
microphone looks healthy and transcribes silence.
"""

import logging
from typing import Optional

from .base import AudioProvider, AudioConfig
from .winrt_capture import WinRTAudioCapture, WINRT_AUDIO_AVAILABLE

logger = logging.getLogger(__name__)

# The refusal a user sees when the WinRT capture path cannot load. David
# approved this exact wording on 2026-09-05, so treat a change to it as a
# user-visible change: it travels to the tray through the provider's
# startup-failed notice. It names the installer first because that is the
# fix for a user, and bootstrap.ps1 second for a developer whose venv is
# out of date.
WINRT_REQUIRED_MESSAGE = (
    "The speech service cannot start: the audio package winsdk is not "
    "installed. Re-run the WheelHouse installer. Developers: run "
    "bootstrap.ps1."
)

# What a provider calls the capture path it took, in its ready
# notification and in the wheelhouse.log line WheelHouse writes from it
# (wh-capture-winrt-required A4). It lives here, beside the one path
# this factory can return, so a provider cannot report a backend the
# factory never built. The refusal above covers the case where winsdk is
# absent; this name is what makes a WRONG path visible instead of
# silent, which is the half a refusal cannot cover -- the 2026-09-05
# defect ran for hours on PortAudio and said nothing.
CAPTURE_BACKEND_NAME = "winrt"

def get_audio_provider(
    config: Optional[AudioConfig] = None,
    overflow_callback=None
) -> AudioProvider:
    """Create the WinRT audio capture, or refuse to create anything.

    Args:
        config: Audio configuration. Defaults to 16kHz mono 30ms.
        overflow_callback: Called when the audio queue overflows.

    Returns:
        A WinRTAudioCapture ready for use. It carries OVERFLOW_SOURCE, the
        words for where this capture loses frames, because a provider that
        writes its own overflow line has only the object this function
        returned to ask.

    Raises:
        RuntimeError: winsdk did not import, so the WinRT AudioGraph is
            unreachable. The message is WINRT_REQUIRED_MESSAGE, and it is
            written for the user rather than for a log reader.

    Example:
        ```python
        provider = get_audio_provider()

        config = AudioConfig(rate=16000, chunk_ms=20)
        provider = get_audio_provider(config=config)
        ```
    """
    if not WINRT_AUDIO_AVAILABLE:
        # No capture is built. Raising after construction would leave a
        # microphone open in a process that is about to exit.
        logger.error(
            "WinRT audio capture is unavailable: winsdk did not import"
        )
        raise RuntimeError(WINRT_REQUIRED_MESSAGE)

    config = config or AudioConfig()
    logger.info("Using WinRT audio capture")
    return WinRTAudioCapture(config, overflow_callback)
