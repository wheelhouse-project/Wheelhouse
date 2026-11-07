"""Samsung lifecycle, command feedback and coordinator integration."""
import asyncio
import re
import time
from unittest.mock import AsyncMock, Mock

import pytest

from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import (HardwareBrightnessCommand, BrightnessStateChanged,
    BrightnessOverflowEvent, SystemConfigurationErrorEvent, BrightnessAdjustCommand)
from services.wheelhouse.integrations.samsung_control import BrightnessChange
from services.wheelhouse.plugins import samsung_plugin as mod
from services.wheelhouse.plugins.base import PluginState
from services.wheelhouse.utils.notice_text import MAX_MESSAGE_WIDE_CHARACTERS, fit_notice_text


class FakeClock:
    """The plugin's clock, advanced by hand, in seconds."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def setup(monkeypatch):
    control = Mock()
    control.get_brightness = AsyncMock(return_value=10)
    control.change_brightness = AsyncMock(return_value=BrightnessChange(12))
    control.last_error = None
    factory = Mock(return_value=control)
    monkeypatch.setattr(mod, 'SamsungControl', factory)
    config = Mock()
    values = {'plugins.samsung.enabled': True, 'plugins.samsung.ip_address': '192.0.2.10',
              'plugins.samsung.credential_file': 'test.dpapi'}
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    return config, values, control, factory, EventBus()


@pytest.mark.asyncio
async def test_command_cascades_only_remainder_to_software_dimmer(setup):
    from services.wheelhouse.coordinators.brightness_coordinator import BrightnessCoordinator
    config, _, control, _, bus = setup
    config.get_config.return_value = {'brightness_coordinator': {'software_dimmer': 'overlay'}}
    dimmer = Mock()
    dimmer.set_brightness.return_value = True
    coordinator = BrightnessCoordinator(config, bus, software_dimmer=dimmer)
    plugin = mod.SamsungPlugin()
    control.get_brightness.return_value = 2
    control.change_brightness.return_value = BrightnessChange(0, -8)
    coordinator.start()
    await plugin.initialize(config, bus)
    await plugin.start()
    try:
        await bus.publish(BrightnessAdjustCommand(-10))
        control.change_brightness.assert_awaited_once_with(-10)
        assert coordinator.get_status()['software_dimmer_level'] == 92
        dimmer.set_brightness.assert_called_with(92)
    finally:
        await plugin.stop()
        coordinator.stop()


@pytest.mark.asyncio
async def test_existing_thumb_wheel_controls_route_to_samsung(setup, monkeypatch):
    from services.wheelhouse.coordinators.brightness_coordinator import BrightnessCoordinator
    from services.wheelhouse.handlers import mouse_handler as mouse_mod

    config, _, control, _, bus = setup
    config.get_config.return_value = {'brightness_coordinator': {'software_dimmer': 'overlay'}}
    monkeypatch.setattr(mouse_mod, 'HIDListener', Mock())
    dimmer = Mock()
    dimmer.set_brightness.return_value = True
    coordinator = BrightnessCoordinator(config, bus, software_dimmer=dimmer)
    handler = mouse_mod.MouseHandler(asyncio.get_running_loop(), config, Mock(),
                                    Mock(), Mock(), dimmer, bus)
    plugin = mod.SamsungPlugin()
    coordinator.start()
    await plugin.initialize(config, bus)
    await plugin.start()
    try:
        # The original input handler accumulates quarter-steps into a TV step.
        await handler._handle_brightness_zone_event(-4)
        control.change_brightness.assert_not_awaited()
        await handler._handle_brightness_zone_event(-4)
        control.change_brightness.assert_awaited_once_with(2)
        assert coordinator.get_status()['plugin_states']['samsung']['level'] == 12
    finally:
        await plugin.stop()
        coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('level', [0, 10, None])
@pytest.mark.parametrize('reason', ['offline', 'timeout', 'rejected'])
async def test_offline_software_dimming_matches_existing_sony_path(setup, level, reason):
    from services.wheelhouse.coordinators.brightness_coordinator import BrightnessCoordinator

    config, _, control, _, bus = setup
    config.get_config.return_value = {'brightness_coordinator': {'software_dimmer': 'overlay'}}
    control.get_brightness.return_value = level
    control.change_brightness.return_value = BrightnessChange(error=reason)
    dimmer = Mock()
    dimmer.set_brightness.return_value = True
    coordinator = BrightnessCoordinator(config, bus, software_dimmer=dimmer)
    plugin = mod.SamsungPlugin()
    clock = FakeClock()
    plugin._monotonic = clock
    coordinator.start()
    await plugin.initialize(config, bus)
    await plugin.start()
    # A failed read at start (level None) opens the interval; this test is about
    # the answer to a step that reaches the TV.
    clock.advance(mod.OFFLINE_RETRY_INTERVAL_SECONDS)
    dimmer.set_brightness.reset_mock()
    try:
        await bus.publish(BrightnessAdjustCommand(-2))
        # An unreachable TV hands a dim step to the software dimmer whatever its
        # last known level (wh-bravia-offline-fallback); a rejection does not.
        if reason in ('offline', 'timeout'):
            dimmer.set_brightness.assert_called_once_with(98)
            # Further dimming stays available through the original coordinator.
            await bus.publish(BrightnessAdjustCommand(-2))
            assert coordinator.get_status()['software_dimmer_level'] == 96
            control.change_brightness.assert_awaited_once_with(-2)
        else:
            dimmer.set_brightness.assert_not_called()
        assert plugin.get_health_status()['details']['tv_connected'] is False
    finally:
        await plugin.stop()
        coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, None])
async def test_disabled_or_absent_never_touches_hardware(setup, enabled):
    config, values, control, factory, bus = setup
    if enabled is None:
        values.clear()
    else:
        values['plugins.samsung.enabled'] = enabled
    plugin = mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    await bus.publish(HardwareBrightnessCommand(2))
    factory.assert_not_called()
    control.get_brightness.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_stop_restart_has_exactly_one_subscription(setup):
    config, _, control, _, bus = setup
    plugin = mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    await plugin.start()
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 1
    await plugin.stop()
    assert plugin.state == PluginState.STOPPED
    await plugin.stop()
    assert plugin.state == PluginState.STOPPED
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 1
    await plugin.start()
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 2
    await plugin.stop()
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 2


@pytest.mark.asyncio
async def test_offline_at_start_remains_running_and_recovers(setup):
    config, _, control, _, bus = setup
    control.get_brightness.return_value = None
    control.last_error = 'timeout'
    clock = FakeClock()
    plugin = mod.SamsungPlugin()
    plugin._monotonic = clock
    await plugin.initialize(config, bus)
    await plugin.start()
    assert plugin.state == PluginState.RUNNING
    assert plugin.get_health_status()['status'] == 'degraded'
    control.last_error = None
    clock.advance(mod.OFFLINE_RETRY_INTERVAL_SECONDS)
    await bus.publish(HardwareBrightnessCommand(2))
    assert plugin.get_health_status()['status'] == 'healthy'


@pytest.mark.asyncio
async def test_partial_overflow_publishes_minimum_before_overflow(setup):
    config, _, control, _, bus = setup
    control.change_brightness.return_value = BrightnessChange(0, -8)
    events = []
    async def capture(event):
        events.append(event)
    bus.subscribe(BrightnessStateChanged, capture)
    bus.subscribe(BrightnessOverflowEvent, capture)
    plugin = mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    events.clear()
    await bus.publish(HardwareBrightnessCommand(-10))
    assert len(events) == 2
    assert isinstance(events[0], BrightnessStateChanged) and events[0].at_min
    assert isinstance(events[1], BrightnessOverflowEvent) and events[1].delta == -8


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['pairing_required', 'rejected', 'unsupported',
                                  'timeout', 'offline', 'readback_mismatch'])
async def test_failures_never_report_success_or_fake_limit(setup, reason):
    config, _, control, _, bus = setup
    clock = FakeClock()
    plugin = mod.SamsungPlugin()
    plugin._monotonic = clock
    await plugin.initialize(config, bus)
    await plugin.start()
    received = []
    async def capture(event):
        received.append(event)
    for event_type in (BrightnessStateChanged, BrightnessOverflowEvent, SystemConfigurationErrorEvent):
        bus.subscribe(event_type, capture)
    control.change_brightness.return_value = BrightnessChange(error=reason)
    await bus.publish(HardwareBrightnessCommand(2))
    offline_handoff = reason in ('timeout', 'offline')
    assert len(received) == (2 if offline_handoff else 1)
    assert isinstance(received[0], SystemConfigurationErrorEvent)
    if offline_handoff:
        assert isinstance(received[1], BrightnessOverflowEvent)
        assert received[1].reason == 'device_offline'
    assert plugin.get_health_status()['details']['error'] == reason
    clock.advance(mod.OFFLINE_RETRY_INTERVAL_SECONDS)
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 2
    assert sum(isinstance(event, SystemConfigurationErrorEvent) for event in received) == 1
    assert not any(isinstance(event, BrightnessStateChanged) for event in received)


@pytest.mark.asyncio
async def test_zero_delta_is_a_noop(setup):
    config, _, control, _, bus = setup
    plugin = mod.SamsungPlugin()
    await plugin.initialize(config, bus)
    await plugin.start()
    await bus.publish(HardwareBrightnessCommand(0))
    control.change_brightness.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_failed_attempt_logs_a_warning_even_when_the_notice_is_deduplicated(setup, caplog):
    config, _, control, _, bus = setup
    clock = FakeClock()
    plugin = mod.SamsungPlugin()
    plugin._monotonic = clock
    await plugin.initialize(config, bus)
    await plugin.start()
    notices = []
    async def capture(event):
        notices.append(event)
    bus.subscribe(SystemConfigurationErrorEvent, capture)
    control.change_brightness.return_value = BrightnessChange(error='timeout')
    caplog.set_level('WARNING', logger=mod.__name__)
    await bus.publish(HardwareBrightnessCommand(-2))
    clock.advance(mod.OFFLINE_RETRY_INTERVAL_SECONDS)
    await bus.publish(HardwareBrightnessCommand(-2))
    lines = [record.getMessage() for record in caplog.records
             if record.name == mod.__name__ and record.levelname == 'WARNING']
    assert len(lines) == 2
    for line in lines:
        assert 'reason=timeout' in line
        assert re.search(r'elapsed=\d+\.\d+s', line)
    assert len(notices) == 1


# wh-wheel-zone-timeout-jump: while the TV does not answer, a wheel step does not
# wait for it. Timeouts took 3.03 s and offline results 2.05 s in the log of
# 2026-11-01, and the wheel batches of that wait queued behind each step.

async def _start_plugin(config, bus, clock):
    plugin = mod.SamsungPlugin()
    plugin._monotonic = clock
    await plugin.initialize(config, bus)
    await plugin.start()
    return plugin


def _capture(bus, *event_types):
    received = []
    async def capture(event):
        received.append(event)
    for event_type in event_types:
        bus.subscribe(event_type, capture)
    return received


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['timeout', 'offline'])
async def test_step_after_a_silent_tv_result_does_not_contact_the_tv(setup, reason):
    config, _, control, _, bus = setup
    clock = FakeClock()
    plugin = await _start_plugin(config, bus, clock)
    control.change_brightness.return_value = BrightnessChange(error=reason)
    await bus.publish(HardwareBrightnessCommand(-2))
    health = plugin.get_health_status()
    received = _capture(bus, BrightnessOverflowEvent, BrightnessStateChanged,
                        SystemConfigurationErrorEvent)
    control.reset_mock()

    await bus.publish(HardwareBrightnessCommand(-4, command_id=4242))

    assert control.method_calls == []
    assert len(received) == 1, 'the step publishes the overflow and nothing else: no second notice'
    overflow = received[0]
    assert isinstance(overflow, BrightnessOverflowEvent)
    assert (overflow.delta, overflow.source_plugin, overflow.reason, overflow.command_id) == (
        -4, 'samsung', 'device_offline', 4242)
    assert plugin.get_health_status() == health


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['timeout', 'offline'])
async def test_skipped_step_takes_the_path_of_a_timed_out_step(setup, reason):
    """Through a real coordinator: a brightening step with no software dimming is
    ignored, and a dim step reaches the software dimmer."""
    from services.wheelhouse.coordinators.brightness_coordinator import BrightnessCoordinator

    config, _, control, _, bus = setup
    config.get_config.return_value = {'brightness_coordinator': {'software_dimmer': 'overlay'}}
    control.change_brightness.return_value = BrightnessChange(error=reason)
    dimmer = Mock()
    dimmer.set_brightness.return_value = True
    coordinator = BrightnessCoordinator(config, bus, software_dimmer=dimmer)
    plugin = mod.SamsungPlugin()
    plugin._monotonic = FakeClock()
    coordinator.start()
    await plugin.initialize(config, bus)
    await plugin.start()
    dimmer.set_brightness.reset_mock()
    try:
        await bus.publish(BrightnessAdjustCommand(2))
        await bus.publish(BrightnessAdjustCommand(2))
        dimmer.set_brightness.assert_not_called()
        await bus.publish(BrightnessAdjustCommand(-2))
        dimmer.set_brightness.assert_called_once_with(98)
        control.change_brightness.assert_awaited_once_with(2)
    finally:
        await plugin.stop()
        coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('last_error, expected', [('timeout', 'timeout'), ('offline', 'offline'),
                                                  (None, 'offline')])
async def test_failed_read_at_start_opens_the_interval(setup, last_error, expected):
    config, _, control, _, bus = setup
    control.get_brightness.return_value = None
    control.last_error = last_error
    clock = FakeClock()
    plugin = await _start_plugin(config, bus, clock)
    assert plugin.get_health_status()['details']['error'] == expected

    await bus.publish(HardwareBrightnessCommand(2))
    control.change_brightness.assert_not_awaited()

    clock.advance(mod.OFFLINE_RETRY_INTERVAL_SECONDS)
    await bus.publish(HardwareBrightnessCommand(2))
    control.change_brightness.assert_awaited_once_with(2)
    assert plugin.get_health_status()['status'] == 'healthy'


@pytest.mark.asyncio
async def test_a_step_at_the_end_of_the_interval_contacts_the_tv(setup):
    config, _, control, _, bus = setup
    clock = FakeClock()
    await _start_plugin(config, bus, clock)
    control.change_brightness.return_value = BrightnessChange(error='timeout')
    await bus.publish(HardwareBrightnessCommand(2))
    failed_at = clock.now

    clock.now = failed_at + mod.OFFLINE_RETRY_INTERVAL_SECONDS - 0.01
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 1

    clock.now = failed_at + mod.OFFLINE_RETRY_INTERVAL_SECONDS
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 2


@pytest.mark.asyncio
async def test_the_interval_starts_when_the_wait_ended(setup):
    """A timed-out step costs 3 seconds. Counted from the start of that wait,
    the interval would be 3 seconds short."""
    config, _, control, _, bus = setup
    clock = FakeClock()
    await _start_plugin(config, bus, clock)

    async def time_out_after_three_seconds(_delta):
        clock.advance(3.0)
        return BrightnessChange(error='timeout')

    control.change_brightness.side_effect = time_out_after_three_seconds
    await bus.publish(HardwareBrightnessCommand(2))

    clock.advance(mod.OFFLINE_RETRY_INTERVAL_SECONDS - 0.01)
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 1


@pytest.mark.asyncio
async def test_a_good_answer_restores_normal_behavior(setup):
    config, _, control, _, bus = setup
    clock = FakeClock()
    plugin = await _start_plugin(config, bus, clock)
    control.change_brightness.return_value = BrightnessChange(error='offline')
    await bus.publish(HardwareBrightnessCommand(2))
    clock.advance(mod.OFFLINE_RETRY_INTERVAL_SECONDS)
    control.change_brightness.return_value = BrightnessChange(12)
    states = _capture(bus, BrightnessStateChanged)

    await bus.publish(HardwareBrightnessCommand(2))

    assert [state.level for state in states] == [12]
    assert plugin.get_health_status()['status'] == 'healthy'
    # No later skip: the next steps contact the TV with the clock standing still.
    # This holds with or without the clear in _publish_state: a step reaches the
    # TV only at or after the end of the interval, the clock never goes back, so
    # an end time left in place cannot skip a step. A good result clears an end
    # time that still lies ahead only through the read at start, which
    # test_a_good_read_at_restart_ends_the_interval guards.
    await bus.publish(HardwareBrightnessCommand(2))
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 4


@pytest.mark.asyncio
async def test_a_failed_retry_opens_a_new_interval(setup):
    config, _, control, _, bus = setup
    clock = FakeClock()
    await _start_plugin(config, bus, clock)
    control.change_brightness.return_value = BrightnessChange(error='timeout')
    await bus.publish(HardwareBrightnessCommand(2))
    clock.advance(mod.OFFLINE_RETRY_INTERVAL_SECONDS)
    await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 2

    await bus.publish(HardwareBrightnessCommand(2))

    assert control.change_brightness.await_count == 2


# Every reason of samsung_control.py that is not a timeout or an offline result. A
# TV that gives one of these answered, so none of them starts or keeps an interval.
_REFUSALS = ['pairing_required', 'identity_mismatch', 'identity_unavailable', 'rejected',
             'unsupported', 'invalid_response', 'readback_mismatch']


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', _REFUSALS)
async def test_a_tv_that_answers_with_a_refusal_ends_the_interval(setup, reason):
    """The TV answered, so the plugin stops leaving it alone."""
    config, _, control, _, bus = setup
    clock = FakeClock()
    await _start_plugin(config, bus, clock)
    control.change_brightness.return_value = BrightnessChange(error='timeout')
    await bus.publish(HardwareBrightnessCommand(2))
    clock.advance(mod.OFFLINE_RETRY_INTERVAL_SECONDS)
    control.change_brightness.return_value = BrightnessChange(error=reason)
    await bus.publish(HardwareBrightnessCommand(2))

    await bus.publish(HardwareBrightnessCommand(2))

    assert control.change_brightness.await_count == 3


@pytest.mark.asyncio
async def test_a_good_read_at_restart_ends_the_interval(setup):
    config, _, control, _, bus = setup
    clock = FakeClock()
    plugin = await _start_plugin(config, bus, clock)
    control.change_brightness.return_value = BrightnessChange(error='timeout')
    await bus.publish(HardwareBrightnessCommand(2))
    await plugin.stop()
    await plugin.start()
    control.change_brightness.return_value = BrightnessChange(12)

    await bus.publish(HardwareBrightnessCommand(2))

    assert control.change_brightness.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('silent', ['timeout', 'offline'])
@pytest.mark.parametrize('reason', _REFUSALS)
async def test_a_refusal_at_restart_ends_the_interval(setup, silent, reason):
    """A step cannot reach the TV inside the interval, so the read at start is the
    only refusal that arrives there. test_a_tv_that_answers_with_a_refusal_ends_the_interval
    gets its refusal at the end of the interval, where a kept end time no longer
    has an effect."""
    config, _, control, _, bus = setup
    clock = FakeClock()
    plugin = await _start_plugin(config, bus, clock)
    control.change_brightness.return_value = BrightnessChange(error=silent)
    await bus.publish(HardwareBrightnessCommand(2))
    await plugin.stop()
    control.get_brightness.return_value = None
    control.last_error = reason
    await plugin.start()

    await bus.publish(HardwareBrightnessCommand(2))

    assert control.change_brightness.await_count == 2


async def _open_the_interval(setup, clock):
    """A plugin inside the interval, and the proof: a step there is skipped."""
    config, _, control, _, bus = setup
    plugin = await _start_plugin(config, bus, clock)
    control.change_brightness.return_value = BrightnessChange(error='timeout')
    await bus.publish(HardwareBrightnessCommand(2))
    overflows = _capture(bus, BrightnessOverflowEvent)
    await bus.publish(HardwareBrightnessCommand(2))
    assert len(overflows) == 1
    assert control.change_brightness.await_count == 1
    return plugin, overflows


@pytest.mark.asyncio
async def test_a_zero_step_during_the_interval_publishes_nothing(setup):
    _, _, _, _, bus = setup
    _, overflows = await _open_the_interval(setup, FakeClock())

    await bus.publish(HardwareBrightnessCommand(0))

    assert len(overflows) == 1


@pytest.mark.asyncio
async def test_a_stopped_plugin_publishes_nothing_during_the_interval(setup):
    _, _, _, _, bus = setup
    plugin, overflows = await _open_the_interval(setup, FakeClock())
    await plugin.stop()

    await bus.publish(HardwareBrightnessCommand(2))

    assert len(overflows) == 1


@pytest.mark.asyncio
async def test_a_step_waiting_behind_a_failing_step_does_not_contact_the_tv(setup):
    """The second step waits for the lock while the first waits for the TV, so
    the plugin must make its check after the lock. A check made before the lock
    sees no failed result yet, and the second step costs a second wait."""
    config, _, control, _, bus = setup
    await _start_plugin(config, bus, FakeClock())
    tv_contacted = asyncio.Event()
    tv_timed_out = asyncio.Event()

    async def tv_that_times_out(_delta):
        tv_contacted.set()
        await tv_timed_out.wait()
        return BrightnessChange(error='timeout')

    control.change_brightness.side_effect = tv_that_times_out
    overflows = _capture(bus, BrightnessOverflowEvent)
    first = asyncio.create_task(bus.publish(HardwareBrightnessCommand(2)))
    await asyncio.wait_for(tv_contacted.wait(), timeout=1.0)
    second = asyncio.create_task(bus.publish(HardwareBrightnessCommand(2)))
    await asyncio.sleep(0.01)

    tv_timed_out.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)

    assert control.change_brightness.await_count == 1
    assert len(overflows) == 2


def test_the_retry_interval_is_ten_seconds():
    """The other tests advance the clock by this name, so they hold for any
    value. Acceptance item 2 asks for a short interval, and item 3 sets it as the
    limit on how long brightness stays unused after the TV answers again."""
    assert mod.OFFLINE_RETRY_INTERVAL_SECONDS == 10.0


def test_the_plugin_clock_is_monotonic():
    """Every other test replaces this clock with a FakeClock."""
    assert mod.SamsungPlugin()._monotonic is time.monotonic


@pytest.mark.asyncio
async def test_an_answering_tv_is_contacted_on_every_step(setup):
    """Acceptance item 4, bounding test, green before and after: one command per
    round trip, with the clock standing still."""
    config, _, control, _, bus = setup
    await _start_plugin(config, bus, FakeClock())
    for _ in range(3):
        await bus.publish(HardwareBrightnessCommand(2))
    assert control.change_brightness.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['timeout', 'offline'])
async def test_silent_tv_notice_names_the_address_fix_and_fits(setup, reason):
    """Acceptance item 5. The GUI shows both fields joined, and text past the
    Windows limit is cut, which would lose the retry sentence at the end."""
    config, _, control, _, bus = setup
    await _start_plugin(config, bus, FakeClock())
    notices = _capture(bus, SystemConfigurationErrorEvent)
    control.change_brightness.return_value = BrightnessChange(error=reason)

    await bus.publish(HardwareBrightnessCommand(2))

    shown = f'{notices[0].error_message}\n\n{notices[0].user_action}'
    for words in ('Its address may have changed', 'router restart',
                  'Reserve the address on the router',
                  'put the new address in Wheelhouse and pair the TV again',
                  'The next command retries.'):
        assert words in shown
    assert fit_notice_text('Samsung TV', shown)[1] == shown
    assert len(shown) <= MAX_MESSAGE_WIDE_CHARACTERS
