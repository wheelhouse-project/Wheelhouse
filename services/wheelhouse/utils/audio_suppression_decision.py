"""Decide once, at startup, whether sound from this computer pauses listening.

wh-audio-suppression-auto.

WHY THIS EXISTS
---------------
Sound this computer plays reaches the microphone, and the recogniser types it
as speech. WheelHouse answered that by pausing listening while the default
output device played sound. On a microphone with an acoustic echo canceller
that pause buys nothing: Windows already removes this computer's own sound
from the captured audio. It costs the user every command spoken over sound,
and a screen reader speaks almost continuously.

So the pause is no longer a switch the user sets. ``ENABLE_AUDIO_SUPPRESSION``
carries "auto" (the shipped default), true or false. Under "auto" this module
asks Windows what capture effects the default microphone gets, and the answer
decides for the session.

WHAT IS ASKED
-------------
``AudioEffectsManager.create_audio_capture_effects_manager`` reports the
effects Windows applies to a capture device for one media category. The
category used here is ``MediaCategory.COMMUNICATIONS`` with
``AudioProcessing.DEFAULT``, which is the category the capture path itself
uses (services/stt_providers/shared/shared_audio/capture/winrt_capture.py
creates its graph with ``AudioRenderCategory.COMMUNICATIONS`` and opens the
input node with ``MediaCategory.COMMUNICATIONS``). Asking about a different
category would describe effects the captured audio never gets.

Two device roles are asked: ``AudioDeviceRole.DEFAULT`` and
``AudioDeviceRole.COMMUNICATIONS`` (boss ruling Q5, 2026-09-17 12:58). On most
machines both name the same microphone. When they differ, the pause is off
only when both report a canceller, because the costly mistake is the one in
the off direction: the user speaks over sound and nothing is recognised.

WHEN WINDOWS CANNOT ANSWER
--------------------------
Every failure -- winsdk missing, a COM error, no default capture device, a
query that does not return within the timeout -- leaves the pause on and logs
one WARNING. The pause is the behaviour WheelHouse shipped for years, so an
unanswered question costs the user nothing new.

WHY A DAEMON THREAD
-------------------
The query is a blocking COM call. It runs on a dedicated daemon thread rather
than through ``asyncio.to_thread``: main.py records (wh-logic-exit-hang) that a
parked default-executor worker hangs ``asyncio.run()`` teardown, so a query
stuck in COM must not be something exit waits for. The event loop awaits a
future the thread resolves, under ``asyncio.wait_for``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass

from services.wheelhouse.utils.winrt_helpers import WinRTError, run_winrt_sync

logger = logging.getLogger(__name__)

AUDIO_SUPPRESSION_SETTING = "ENABLE_AUDIO_SUPPRESSION"
AUDIO_SUPPRESSION_DEFAULT = "auto"

# The whole query -- two device ids, two effects managers and two name
# lookups -- gets this long. A machine that cannot answer in five seconds is
# treated as a machine that cannot answer (boss ruling Q7).
QUERY_TIMEOUT_SECONDS = 5.0

# One device-name lookup. The name is for the log line only, so it gets far
# less than the whole query does.
NAME_LOOKUP_TIMEOUT_SECONDS = 2.0

QUERY_THREAD_NAME = "audio-suppression-query"

ROLE_DEFAULT = "default"
ROLE_COMMUNICATIONS = "communications"

_WITH_CANCELLER = "an echo canceller"
_WITHOUT_CANCELLER = "no echo canceller"


@dataclass(frozen=True)
class RoleReport:
    """What Windows says about the default capture device of one role."""

    role: str
    device_name: str
    has_echo_canceller: bool


@dataclass(frozen=True)
class EchoCancellerReport:
    """What Windows says about both device roles."""

    roles: tuple[RoleReport, ...]

    @property
    def has_echo_canceller(self) -> bool:
        """True only when every role asked reports a canceller (ruling Q5)."""
        return bool(self.roles) and all(
            role.has_echo_canceller for role in self.roles
        )

    def describe(self) -> str:
        """The part of the INFO line that names what Windows reported.

        One device and one answer reads as acceptance criterion 2 writes it:
        "an echo canceller for Microphone (2- Scarlett Solo USB)". Two
        different devices name both, and two different answers say which
        device gave which answer.
        """
        answers = {role.has_echo_canceller for role in self.roles}
        names = {role.device_name for role in self.roles}
        phrase = _WITH_CANCELLER if self.has_echo_canceller else _WITHOUT_CANCELLER

        if len(answers) == 1 and len(names) == 1:
            return f"{phrase} for {self.roles[0].device_name}"
        if len(answers) == 1:
            named = " and ".join(
                f"{role.device_name} ({role.role})" for role in self.roles
            )
            return f"{phrase} for {named}"
        return " and ".join(
            f"{_WITH_CANCELLER if role.has_echo_canceller else _WITHOUT_CANCELLER}"
            f" for {role.device_name} ({role.role})"
            for role in self.roles
        )


def parse_audio_suppression_setting(value) -> bool | None:
    """Read the setting: True forces on, False forces off, None means "auto".

    Boss ruling Q4 (2026-09-17 12:58): TOML true and false, plus the strings
    "auto", "true" and "false" in any letter case and with surrounding spaces.
    Anything else gives one WARNING that names the accepted values, and "auto"
    is used -- the value that asks Windows rather than one that guesses.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().casefold()
        if cleaned == "auto":
            return None
        if cleaned == "true":
            return True
        if cleaned == "false":
            return False
    logger.warning(
        '%s is %r; expected "auto", true or false. Using "auto".',
        AUDIO_SUPPRESSION_SETTING,
        value,
    )
    return None


def _device_name(device_id: str) -> str:
    """The friendly name of a capture device, or its id when that fails.

    Boss ruling Q8: a failed lookup logs the device id. The name is for the
    reader of the log line, so a failure here must not change the decision.
    """
    from winsdk.windows.devices.enumeration import DeviceInformation

    try:
        information = run_winrt_sync(
            DeviceInformation.create_from_id_async(device_id),
            timeout=NAME_LOOKUP_TIMEOUT_SECONDS,
        )
        return information.name
    except Exception as error:  # noqa: BLE001 -- the id is a usable name
        logger.debug("Capture device %s has no readable name: %s", device_id, error)
        return device_id


def query_echo_canceller() -> EchoCancellerReport:
    """Ask Windows whether the default microphone has an echo canceller.

    Blocking, and it must stay blocking: it runs on the daemon thread that
    ``decide_audio_suppression`` starts. winsdk is imported here rather than
    at module import so that a missing package is one failed decision instead
    of a Logic process that cannot start.

    Raises:
        WinRTError: when Windows names no default capture device for a role.
        Anything winsdk raises: an ImportError, a COM error, an OSError.
    """
    from winsdk.windows.media import AudioProcessing
    from winsdk.windows.media.capture import MediaCategory
    from winsdk.windows.media.devices import AudioDeviceRole, MediaDevice
    from winsdk.windows.media.effects import AudioEffectsManager, AudioEffectType

    reports: list[RoleReport] = []
    for role_name, role in (
        (ROLE_DEFAULT, AudioDeviceRole.DEFAULT),
        (ROLE_COMMUNICATIONS, AudioDeviceRole.COMMUNICATIONS),
    ):
        device_id = MediaDevice.get_default_audio_capture_id(role)
        if not device_id:
            raise WinRTError(
                f"Windows names no default capture device for the {role_name} role"
            )
        manager = AudioEffectsManager.create_audio_capture_effects_manager(
            device_id, MediaCategory.COMMUNICATIONS, AudioProcessing.DEFAULT
        )
        has_canceller = any(
            effect.audio_effect_type == AudioEffectType.ACOUSTIC_ECHO_CANCELLATION
            for effect in manager.get_audio_capture_effects()
        )
        reports.append(
            RoleReport(role_name, _device_name(device_id), has_canceller)
        )
    return EchoCancellerReport(tuple(reports))


def _run_on_daemon_thread(query) -> asyncio.Future:
    """Start `query` on a daemon thread and return the future it resolves."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def deliver(result, error) -> None:
        # The awaiting side may have given up: wait_for cancels the future on
        # a timeout, and setting a result on a cancelled future raises.
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def work() -> None:
        try:
            result, error = query(), None
        except Exception as caught:  # noqa: BLE001 -- every failure is a decision
            result, error = None, caught
        try:
            loop.call_soon_threadsafe(deliver, result, error)
        except RuntimeError:
            # The loop closed while the query ran. Nothing is waiting.
            pass

    threading.Thread(
        target=work, name=QUERY_THREAD_NAME, daemon=True
    ).start()
    return future


def _failure_detail(error: BaseException) -> str:
    """A readable one-line description; a bare TimeoutError has no message."""
    text = str(error)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


# crewcut: Windows is asked once, at startup, about the default microphone.
# A microphone plugged in or made the default later is not checked until
# the next start. Removing the limit means subscribing to the
# DefaultAudioCaptureDeviceChanged event of Windows.Media.Devices.MediaDevice
# (winsdk: MediaDevice.add_default_audio_capture_device_changed) and asking
# again when it fires.
async def decide_audio_suppression(
    value,
    *,
    query=query_echo_canceller,
    timeout: float = QUERY_TIMEOUT_SECONDS,
) -> bool:
    """Answer whether sound from this computer pauses listening this session.

    Args:
        value: what ``ENABLE_AUDIO_SUPPRESSION`` holds.
        query: the blocking Windows query; replaced in tests.
        timeout: how long the query gets before it counts as unanswered.

    Returns:
        True when sound pauses listening, False when it does not.
    """
    forced = parse_audio_suppression_setting(value)
    if forced is True:
        logger.info("Audio suppression on: %s is true", AUDIO_SUPPRESSION_SETTING)
        return True
    if forced is False:
        logger.info("Audio suppression off: %s is false", AUDIO_SUPPRESSION_SETTING)
        return False

    started = time.monotonic()
    try:
        report = await asyncio.wait_for(_run_on_daemon_thread(query), timeout)
    except Exception as error:  # noqa: BLE001 -- includes TimeoutError
        logger.warning(
            "Audio suppression on: could not ask Windows whether the microphone "
            "has an echo canceller (%s)",
            _failure_detail(error),
        )
        return True

    # Boss ruling Q7: the manual-test checklist quotes this duration.
    logger.info(
        "Audio suppression query took %.2f s", time.monotonic() - started
    )
    if report.has_echo_canceller:
        logger.info("Audio suppression off: Windows reports %s", report.describe())
        return False
    logger.info("Audio suppression on: Windows reports %s", report.describe())
    return True
