"""Opt-in Samsung TV brightness plugin using the existing coordinator events."""
import asyncio
import logging
from pathlib import Path
import time
from typing import Callable, Optional

from services.wheelhouse.events import (HardwareBrightnessCommand, BrightnessStateChanged,
    BrightnessOverflowEvent, SystemConfigurationErrorEvent)
from services.wheelhouse.integrations.samsung_control import SamsungControl
from services.wheelhouse.plugins.base import BasePlugin, PluginState


# How long the plugin leaves the TV alone after a timeout or offline result. Each
# such result costs a wait (2.05 to 3 seconds in the 2026-11-01 log), and the
# thumb wheel batches rolled during it queue up behind the step
# (wh-wheel-zone-timeout-jump). Trade-off: during this interval a TV that came
# back stays unused, and a wheel step costs at most one wait per interval.
OFFLINE_RETRY_INTERVAL_SECONDS = 10.0

# The results of a TV that did not answer at all, as against one that refused.
_NO_ANSWER_ERRORS = ('offline', 'timeout')

# The GUI shows f'{error_message}\n\n{user_action}', and utils/notice_text.py cuts
# text beyond MAX_MESSAGE_WIDE_CHARACTERS (255). The retry sentence is at the end,
# so the whole message must fit.
_NO_ANSWER_ACTION = ('Check that the TV is on. Its address may have changed, for example '
                     'after a router restart. Reserve the address on the router, or put '
                     'the new address in Wheelhouse and pair the TV again. The next '
                     'command retries.')

_ERRORS = {
    'pairing_required': ('Samsung TV pairing is missing or was refused.', 'Run Samsung TV pairing again.'),
    'identity_mismatch': ('The configured Samsung TV identity has changed.', 'Check the selected TV and pair it again.'),
    'identity_unavailable': ('The Samsung TV identity could not be checked.', 'Check that the TV is on and reachable.'),
    'unsupported': ('This Samsung TV did not accept the brightness method.', 'Check the TV picture mode and supported model information.'),
    'rejected': ('The Samsung TV rejected the brightness change.', 'Check the TV picture mode; try again after the setting becomes available.'),
    'timeout': ('The Samsung TV did not respond in time.', _NO_ANSWER_ACTION),
    'offline': ('The Samsung TV connection is unavailable.', _NO_ANSWER_ACTION),
    'invalid_response': ('The Samsung TV returned an unexpected response.', 'Check the TV firmware and pairing.'),
    'readback_mismatch': ('The Samsung TV did not apply the requested brightness.', 'Check the TV picture mode and automatic brightness settings.'),
}


logger = logging.getLogger(__name__)


class SamsungPlugin(BasePlugin):
    default_enabled = False

    def __init__(self):
        super().__init__()
        self._control = None
        self._event_bus = None
        self._active = False
        self._subscribed = False
        self._lock = asyncio.Lock()
        self._brightness = None
        self._error = None
        self._last_check = None
        # Monotonic time before which the plugin does not contact the TV; None
        # when the last result was not a timeout or offline result.
        self._retry_after: Optional[float] = None
        self._monotonic: Callable[[], float] = time.monotonic

    @property
    def name(self) -> str:
        return 'samsung'

    async def initialize(self, config, event_bus) -> None:
        self._event_bus = event_bus
        self._active = config.get('plugins.samsung.enabled', False) is True
        if self._active:
            address = config.get('plugins.samsung.ip_address', '')
            credential = config.get('plugins.samsung.credential_file', '')
            if not isinstance(credential, str) or not credential.strip():
                raise ValueError('plugins.samsung.credential_file must name the saved pairing file')
            self._control = SamsungControl(address, Path(credential).expanduser())
        self._state = PluginState.INITIALIZED

    async def start(self) -> None:
        async with self._lock:
            if self._state == PluginState.RUNNING:
                return
            self._state = PluginState.RUNNING
            if not self._active:
                return
            if not self._subscribed:
                self._event_bus.subscribe(HardwareBrightnessCommand, self._handle_brightness_command)
                self._subscribed = True
            value = await self._control.get_brightness()
            if value is None:
                await self._report_error(self._control.last_error or 'offline')
            else:
                await self._publish_state(value)

    async def stop(self) -> None:
        self._state = PluginState.STOPPING
        # EventBus has no unsubscribe API. Keep one registration for this instance;
        # the handler ignores stopped commands, and start() reuses the registration.
        async with self._lock:
            self._state = PluginState.STOPPED

    async def _handle_brightness_command(self, event: HardwareBrightnessCommand) -> None:
        async with self._lock:
            if not self._active or self._state != PluginState.RUNNING or event.delta == 0:
                return
            if self._retry_after is not None and self._monotonic() < self._retry_after:
                logger.debug(f"Samsung TV not contacted: last result was {self._error}, "
                             f"retry in {self._retry_after - self._monotonic():.1f}s")
                await self._publish_offline_overflow(event)
                return
            started = time.monotonic()
            result = await self._control.change_brightness(event.delta)
            if result.error:
                # The notice below is sent only when the reason changes; this line
                # records every failed attempt.
                logger.warning(f"Samsung brightness attempt failed: reason={result.error}, "
                               f"elapsed={time.monotonic() - started:.2f}s")
                await self._report_error(result.error)
                if result.error in _NO_ANSWER_ERRORS:
                    await self._publish_offline_overflow(event)
                return
            await self._publish_state(result.level, command_id=event.command_id)
            if result.overflow:
                # Coordinator must know the actual limit before it sees overflow.
                await self._event_bus.publish(BrightnessOverflowEvent(
                    delta=result.overflow, source_plugin=self.name, reason='at_hardware_limit',
                    command_id=event.command_id))

    async def _publish_offline_overflow(self, event: HardwareBrightnessCommand) -> None:
        # Match Sony's offline handoff. The coordinator gives a dim step
        # to the software dimmer whatever the last known hardware state.
        await self._event_bus.publish(BrightnessOverflowEvent(
            delta=event.delta, source_plugin=self.name, reason='device_offline',
            command_id=event.command_id))

    async def _publish_state(self, value: int, command_id: Optional[int] = None) -> None:
        self._brightness = value
        self._error = None
        self._retry_after = None
        self._last_check = time.time()
        await self._event_bus.publish(BrightnessStateChanged(
            level=value, at_min=value == 0, at_max=value == 100, source_plugin=self.name,
            command_id=command_id))

    async def _report_error(self, reason: str) -> None:
        previous = self._error
        self._brightness = None
        self._error = reason
        self._last_check = time.time()
        # Read here, after the failed result returned, so the interval starts
        # when the wait ended.
        self._retry_after = (self._monotonic() + OFFLINE_RETRY_INTERVAL_SECONDS
                             if reason in _NO_ANSWER_ERRORS else None)
        if reason != previous:
            message, action = _ERRORS.get(reason, _ERRORS['invalid_response'])
            await self._event_bus.publish(SystemConfigurationErrorEvent(
                service_name='Samsung TV', error_message=message, user_action=action))

    def get_health_status(self) -> dict:
        running = self._state == PluginState.RUNNING
        status = 'healthy' if running and (not self._active or self._brightness is not None) else 'degraded'
        if self._state == PluginState.FAILED:
            status = 'unhealthy'
        return {'status': status, 'state': self._state.value, 'last_check': self._last_check,
                'details': {'active': self._active, 'tv_connected': self._brightness is not None,
                            'last_brightness': self._brightness, 'error': self._error}}
