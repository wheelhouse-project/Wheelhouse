"""Synthetic IMMDevice endpoints through real plugin/monitor/PTT event handlers."""
import asyncio
import threading
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from event_bus import EventBus
from services.wheelhouse.events import (
    PTTStartedEvent, PTTStoppedEvent, PTTMuteStateEvent, SystemConfigurationErrorEvent,
    VolumeAdjustCommand,
)
from services.wheelhouse.handlers.audio_monitor import AudioMonitor
from services.wheelhouse.plugins import system_volume_plugin as volume
from services.wheelhouse.state_manager import StateManager


class Endpoint:
    def __init__(self, identifier):
        self.identifier = identifier
        self.db = -10.0
        self.writes = []
        self.identity_error = None
        self.mute_error = None
        self.identity_hook = None

    def GetId(self):
        if self.identity_hook:
            self.identity_hook()
        if self.identity_error:
            raise self.identity_error
        return self.identifier

    def Activate(self, *args):
        return self

    def QueryInterface(self, interface):
        return self

    def GetPeakValue(self):
        return 0.8 if self.db > -60 else 0.0

    def GetMasterVolumeLevel(self):
        return self.db

    def SetMasterVolumeLevel(self, db, context):
        if self.mute_error:
            raise self.mute_error
        self.db = db
        self.writes.append(db)


class Timer:
    def __init__(self, callback):
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


@pytest.fixture(autouse=True)
def app_data_dir(tmp_path, monkeypatch):
    """Keep the push-to-talk volume record out of the real %APPDATA%.

    Every hold the real plugin mutes writes that record in the directory from
    utils.system.get_app_data_path (wh-ptt-mute-orphaned-on-process-loss).
    """
    from services.wheelhouse.utils import system
    directory = tmp_path / "app_data"
    directory.mkdir()
    monkeypatch.setattr(system, "get_app_data_path", lambda: str(directory))
    return directory


@pytest.fixture
def rig(monkeypatch, mock_config, mock_gui_queue, mock_websocket_manager):
    multimedia, communications = Endpoint("speakers-id"), Endpoint("headset-id")
    roles = {"default": multimedia, "communications": communications}
    def communications_endpoint(flow, role):
        assert flow == volume.EDataFlow.eRender.value
        assert role == volume.ERole.eCommunications.value
        result = roles["communications"]
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(volume.AudioUtilities, "GetSpeakers", lambda: roles["default"])
    monkeypatch.setattr(volume.AudioUtilities, "GetDeviceEnumerator", lambda: SimpleNamespace(
        GetDefaultAudioEndpoint=communications_endpoint))
    scheduled = deque()
    loop = Mock()
    loop.create_task.side_effect = scheduled.append
    loop.call_later.side_effect = lambda seconds, callback: Timer(callback)
    bus = EventBus()
    sm = StateManager(mock_config, bus, loop, mock_gui_queue, mock_websocket_manager)
    sm.speech_notifier = Mock()
    monitor = AudioMonitor.__new__(AudioMonitor)
    monitor._audio_suspended = False
    plugin = volume.SystemVolumePlugin()
    plugin._event_bus = bus
    bus.subscribe(PTTStartedEvent, plugin._handle_ptt_started)
    bus.subscribe(PTTStoppedEvent, plugin._handle_ptt_stopped)
    reports = []
    async def record(event):
        reports.append(event)
    bus.subscribe(PTTMuteStateEvent, record)
    config_errors = []
    async def record_config_error(event):
        config_errors.append(event)
    bus.subscribe(SystemConfigurationErrorEvent, record_config_error)

    async def drain():
        while scheduled:
            await scheduled.popleft()
    async def start(device_type="communications"):
        plugin._device_type = device_type
        await plugin._connect_audio_device()
        sm._speech_enabled = True
        sm.set_speech_suppressed_by_audio(monitor.is_audio_playing())
        assert sm.speech_enabled is False, "the metered speakers must be playing before PTT"
        sm.ptt_start()
        await drain()
    yield SimpleNamespace(sm=sm, plugin=plugin, monitor=monitor, start=start, drain=drain,
                          roles=roles, speakers=multimedia, headset=communications,
                          reports=reports, config_errors=config_errors,
                          websocket=mock_websocket_manager, gui=mock_gui_queue)
    for coroutine in scheduled:
        coroutine.close()


@pytest.mark.asyncio
async def test_distinct_communications_mute_cannot_enable_speech(rig):
    await rig.start()
    assert rig.monitor.is_audio_playing(), "the independent multimedia speakers still play"
    assert rig.sm.speech_enabled is False, "a different endpoint must not authorize listening over speakers"
    assert rig.sm._ptt_audio_override is False
    assert len(rig.reports) == 1 and rig.reports[0].muted is False
    assert rig.reports[0].reason == "endpoint_mismatch"
    assert rig.websocket.set_transcription_status.call_args.args[0] is False
    assert rig.gui.put_nowait.called, "the hands-free UI must receive the correction"
    assert rig.headset.writes == [rig.plugin._min_volume_db], \
        "the configured device is still silenced for the hold; only the override is withheld"
    await rig.plugin._handle_volume_adjust(VolumeAdjustCommand(delta=1))
    assert rig.headset.db == rig.plugin._min_volume_db, \
        "a wheel turn during the hold waits for the release, as it does on a matching endpoint"
    rig.sm.ptt_stop()
    await rig.drain()
    assert rig.headset.db == -7.0, "the release restores the configured device and keeps the wheel turn"
    assert rig.speakers.db == -10.0


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["default", "same-role", "fallback"])
async def test_matching_endpoint_keeps_default_ptt_behavior(rig, selection):
    if selection == "same-role":
        rig.roles["communications"] = rig.speakers
    elif selection == "fallback":
        rig.roles["communications"] = OSError("role unavailable")
    await rig.start("default" if selection == "default" else "communications")
    assert rig.reports[0].muted is True
    assert rig.sm.speech_enabled is True
    assert rig.monitor.is_audio_playing() is False
    rig.sm.ptt_stop()
    await rig.drain()
    assert rig.speakers.db == -10.0, "release must restore the same endpoint"


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", [None, ""])
async def test_unverifiable_connected_identity_refuses_override(rig, identifier):
    rig.speakers.identifier = identifier
    await rig.start("default")
    assert rig.reports[0].muted is False, "an unknown identity is not evidence of a safe endpoint"
    assert rig.sm.speech_enabled is False
    assert rig.speakers.writes == []


@pytest.mark.asyncio
async def test_identity_read_failure_does_not_remove_volume_control(rig):
    rig.speakers.identity_error = OSError("endpoint identity unavailable")
    await rig.start("default")
    assert rig.reports[0].muted is False, "failed identity lookup must not authorize the override"
    assert rig.sm.speech_enabled is False
    await rig.plugin._handle_volume_adjust(VolumeAdjustCommand(delta=1))
    assert rig.speakers.db == -7.0


@pytest.mark.asyncio
async def test_default_device_change_is_re_resolved_on_the_next_press(rig):
    rig.plugin._device_type = "default"
    await rig.plugin._connect_audio_device()
    rig.roles["default"] = rig.headset
    rig.sm._speech_enabled = True
    rig.sm.set_speech_suppressed_by_audio(rig.monitor.is_audio_playing())
    rig.sm.ptt_start()
    await rig.drain()
    assert rig.reports[0].muted is True, "a moved default endpoint must be re-resolved, not refused for the session"
    assert rig.headset.writes == [rig.plugin._min_volume_db], "the endpoint now metered is the one turned down"
    assert rig.speakers.writes == [], "the endpoint that stopped being the default must not be touched"
    assert rig.sm.speech_enabled is True
    rig.sm.ptt_stop()
    await rig.drain()
    assert rig.headset.db == -10.0, "the release restores the re-resolved endpoint"


@pytest.mark.asyncio
async def test_identity_read_failure_is_retried_on_the_next_press(rig):
    rig.plugin._device_type = "default"
    rig.speakers.identity_error = OSError("endpoint identity unavailable")
    await rig.plugin._connect_audio_device()
    assert rig.plugin._audio_device_id is None, "the connect could not read the identity"
    rig.speakers.identity_error = None
    rig.sm._speech_enabled = True
    rig.sm.set_speech_suppressed_by_audio(rig.monitor.is_audio_playing())
    rig.sm.ptt_start()
    await rig.drain()
    assert rig.reports[0].muted is True, "one unreadable identity must not disable the override for the session"
    assert rig.speakers.writes == [rig.plugin._min_volume_db]
    assert rig.sm.speech_enabled is True


@pytest.mark.asyncio
async def test_health_reports_a_persisting_endpoint_refusal_and_the_next_success_clears_it(rig):
    await rig.start()
    assert rig.reports[0].reason == "endpoint_mismatch"
    assert rig.plugin.get_health_status()["error"], "a refusal that costs the user the override must reach health"
    rig.sm.ptt_stop()
    await rig.drain()
    rig.roles["communications"] = rig.speakers
    rig.sm.ptt_start()
    await rig.drain()
    assert rig.reports[-1].muted is True, "the refusal must be retried, not remembered as permanent"
    assert rig.plugin.get_health_status()["error"] is None, "the next successful mute clears the recorded error"


@pytest.mark.asyncio
async def test_reconnect_cannot_reuse_an_old_endpoint_identity(rig):
    rig.plugin._device_type = "communications"
    rig.roles["communications"] = rig.speakers
    await rig.plugin._connect_audio_device()
    rig.roles["communications"] = rig.headset
    rig.headset.identity_error = OSError("new endpoint identity unavailable")
    await rig.plugin._connect_audio_device()
    rig.sm._speech_enabled = True
    rig.sm.set_speech_suppressed_by_audio(rig.monitor.is_audio_playing())
    rig.sm.ptt_start()
    await rig.drain()
    assert rig.reports[0].muted is False, "reconnection must discard the old endpoint identity"
    assert rig.sm.speech_enabled is False and rig.headset.writes == []


@pytest.mark.asyncio
async def test_a_success_without_a_reconnect_still_clears_the_recorded_error(rig):
    """The companion of the test above, for the success that re-resolves nothing.

    A press whose endpoints already agree never calls _connect_audio_device, so
    the mute itself is the only thing that can clear the error a refusal
    recorded. Reaching that success needs the metered default to move ONTO the
    configured device rather than the plugin to follow it.
    """
    await rig.start()
    assert rig.plugin.get_health_status()["error"], "the mismatch was recorded"
    rig.sm.ptt_stop()
    await rig.drain()
    rig.roles["default"] = rig.headset
    rig.sm.ptt_start()
    await rig.drain()
    assert rig.reports[-1].muted is True, "Windows now plays through the configured device"
    assert rig.plugin._audio_device_id == rig.headset.identifier, "and it did so on the cached endpoint"
    assert rig.plugin.get_health_status()["error"] is None, "the successful mute clears the recorded error"


@pytest.mark.asyncio
async def test_a_configured_endpoint_refusal_is_reported_to_the_user(rig):
    await rig.start()
    assert rig.reports[0].reason == "endpoint_mismatch"
    assert len(rig.config_errors) == 1, "the log is not a channel this audience reads"
    notice = rig.config_errors[0]
    assert notice.service_name == "SystemVolumePlugin"
    assert "communications" in notice.error_message, "the notice must name the configured device"
    assert "endpoint_mismatch" in notice.error_message, "and the reason it refused"
    assert "plugins.system_volume.device_type" in notice.user_action


@pytest.mark.asyncio
async def test_a_repeated_refusal_does_not_repeat_the_notice(rig):
    await rig.start()
    assert len(rig.config_errors) == 1
    rig.sm.ptt_stop()
    await rig.drain()
    rig.sm.ptt_start()
    await rig.drain()
    assert rig.reports[-1].reason == "endpoint_mismatch", "the second press refuses for the same reason"
    assert len(rig.config_errors) == 1, "one notice per reason, not one per press"


@pytest.mark.asyncio
async def test_a_successful_mute_re_arms_the_configured_endpoint_notice(rig):
    """The re-arm runs on endpoint_mismatch, the one reason that still reports.

    endpoint_unverified stopped publishing the notice
    (wh-codex-merge-audit.6.2.2), so a multimedia default that moves onto the
    configured device and away again is what exercises the clearing now.
    """
    await rig.start()
    assert rig.reports[-1].reason == "endpoint_mismatch"
    assert len(rig.config_errors) == 1
    rig.sm.ptt_stop()
    await rig.drain()
    rig.roles["default"] = rig.headset
    rig.sm.ptt_start()
    await rig.drain()
    assert rig.reports[-1].muted is True, "the retry succeeds once the endpoints agree"
    rig.sm.ptt_stop()
    await rig.drain()
    rig.roles["default"] = rig.speakers
    rig.sm.ptt_start()
    await rig.drain()
    assert rig.reports[-1].reason == "endpoint_mismatch"
    assert len(rig.config_errors) == 2, "a refusal after a successful mute is a new fault to report"


@pytest.mark.asyncio
async def test_mute_failure_keeps_the_existing_failure_feedback(rig):
    rig.speakers.mute_error = OSError("mute refused")
    await rig.start("default")
    assert rig.reports[0].reason == "error"
    assert rig.sm.speech_enabled is False
    assert rig.websocket.set_transcription_status.call_args.args[0] is False


@pytest.mark.asyncio
async def test_identity_timeout_withdraws_override_and_late_ack_cannot_restore_it(rig):
    rig.plugin._device_type = "default"
    await rig.plugin._connect_audio_device()
    started, release = threading.Event(), threading.Event()
    def slow_identity():
        started.set()
        release.wait(2)
    rig.speakers.identity_hook = slow_identity
    rig.sm._speech_enabled = True
    rig.sm.set_speech_suppressed_by_audio(True)
    rig.sm.ptt_start()
    drain_task = asyncio.create_task(rig.drain())
    try:
        assert await asyncio.to_thread(started.wait, 1), "the endpoint check must actually run"
        rig.sm._ptt_mute_confirm_handle.fire()
        assert rig.sm.speech_enabled is False
    finally:
        release.set()
        await drain_task
    assert rig.sm._ptt_audio_override is False
    assert rig.sm.speech_enabled is False, "a late identity/mute answer cannot reopen the microphone"
    rig.sm.ptt_stop()
    await rig.drain()
    assert rig.speakers.db == -10.0


@pytest.mark.asyncio
async def test_a_failed_re_resolve_keeps_the_identity_for_the_next_press(rig, monkeypatch):
    """A failing re-resolve must not cost the plugin the identity it already had.

    device_type = "communications" on a machine whose communications endpoint
    is not the multimedia default re-resolves on EVERY press, so one transient
    COM failure there decides a real hold. _connect_audio_device clears the
    identity before it tries anything and never puts it back, while the
    interface it failed to replace is still live (wh-codex-merge-audit.6.2.1).
    This press writes no volume level, because the re-resolve never told it
    which endpoint that interface controls now (wh-codex-merge-audit.6.2.3).
    """
    rig.plugin._device_type = "communications"
    await rig.plugin._connect_audio_device()
    assert rig.plugin._audio_device_id == rig.headset.identifier

    def unavailable_enumerator():
        raise OSError("device enumerator unavailable")

    monkeypatch.setattr(volume.AudioUtilities, "GetDeviceEnumerator", unavailable_enumerator)
    rig.sm._speech_enabled = True
    rig.sm.set_speech_suppressed_by_audio(rig.monitor.is_audio_playing())
    rig.sm.ptt_start()
    await rig.drain()
    assert rig.plugin._audio_device_id == rig.headset.identifier, \
        "a failed re-resolve must leave the identity of the live interface alone"
    assert rig.headset.writes == [], \
        "an endpoint the press could not re-confirm is not turned down"
    assert rig.reports[-1].reason == "endpoint_unverified"
    assert rig.sm.speech_enabled is False, "the override stays withheld while the speakers play"


@pytest.mark.asyncio
async def test_an_unverifiable_endpoint_is_not_reported_as_a_configuration_error(rig):
    """An unreadable render endpoint is a device condition, not a setting.

    It is the same AudioUtilities.GetSpeakers() failure
    handlers/audio_monitor.py reports as "Audio device unavailable", and under
    the shipped device_type = "default" every clause of the notice's
    user_action is a no-op for it. The refusal keeps its health surface
    (wh-codex-merge-audit.6.2.2).
    """
    rig.plugin._device_type = "default"
    await rig.plugin._connect_audio_device()
    rig.speakers.identity_error = OSError("endpoint identity unavailable")
    rig.sm._speech_enabled = True
    rig.sm.set_speech_suppressed_by_audio(rig.monitor.is_audio_playing())
    rig.sm.ptt_start()
    await rig.drain()
    assert rig.reports[-1].reason == "endpoint_unverified"
    assert rig.config_errors == [], "an unreadable endpoint is not a configuration error"
    assert rig.plugin.get_health_status()["error"] == \
        "Push-to-talk audio endpoint identity is unverified", \
        "the refusal is still recorded where health reads it"


@pytest.mark.asyncio
async def test_a_failed_re_resolve_is_not_reported_as_a_configured_device_mismatch(rig, monkeypatch):
    """The re-resolve raised, so which endpoint the cached interface holds is unknown.

    device_type = "default" is what ships (config.toml:52). A render endpoint
    arrives, Windows moves the multimedia default onto it, and that same
    arrival is what makes the re-resolve fail. Turning the cached interface
    down then silences speakers the user is not listening to and leaves the
    ones that are playing alone, and calling it an endpoint mismatch tells a
    device_type = "default" user to make the default device the default
    (wh-codex-merge-audit.6.2.3).
    """
    rig.plugin._device_type = "default"
    await rig.plugin._connect_audio_device()
    assert rig.plugin._audio_device_id == rig.speakers.identifier
    rig.roles["default"] = rig.headset

    def the_arriving_endpoint_cannot_be_resolved():
        # AudioUtilities.GetSpeakers() raising inside _get_audio_device is
        # swallowed there and returns None, which is what makes
        # _connect_audio_device raise before it touches _volume_interface.
        return None

    monkeypatch.setattr(rig.plugin, "_get_audio_device", the_arriving_endpoint_cannot_be_resolved)
    rig.sm._speech_enabled = True
    rig.sm.set_speech_suppressed_by_audio(rig.monitor.is_audio_playing())
    rig.sm.ptt_start()
    await rig.drain()
    assert rig.plugin._volume_interface is rig.speakers, "the failed re-resolve kept the old interface"
    assert rig.reports[-1].reason == "endpoint_unverified", \
        "a re-resolve that failed is not a configured-device mismatch"
    assert rig.config_errors == [], "no setting the user can change corrects a failed re-resolve"
    assert rig.speakers.writes == [], "an endpoint the press could not confirm is not turned down"
    assert rig.headset.writes == []
    assert rig.plugin.get_health_status()["error"], "the refusal is still recorded where health reads it"
    assert rig.sm.speech_enabled is False


@pytest.mark.asyncio
async def test_a_replaced_interface_never_inherits_the_previous_identity(rig, monkeypatch):
    """Activate succeeded and GetId did not, so the new interface has no name.

    _connect_audio_device replaces self._volume_interface before it reads the
    new endpoint's identity and swallows a GetId failure, so it can return
    with a live interface and no identity. Putting the previous identity back
    onto that interface names an endpoint it no longer controls
    (wh-codex-merge-audit.6.2.3).
    """
    rig.plugin._device_type = "default"
    await rig.plugin._connect_audio_device()
    assert rig.plugin._audio_device_id == rig.speakers.identifier
    rig.roles["default"] = Endpoint("arriving-id")
    # AudioUtilities.GetSpeakers() hands out a fresh IMMDevice for the same
    # endpoint on every call. This is the one the re-resolve activates, and
    # the one whose GetId fails while the metered read's copy still answers.
    arriving_view = Endpoint("arriving-id")
    arriving_view.identity_error = OSError("new endpoint identity unavailable")
    monkeypatch.setattr(rig.plugin, "_get_audio_device", lambda: arriving_view)
    rig.sm._speech_enabled = True
    rig.sm.set_speech_suppressed_by_audio(rig.monitor.is_audio_playing())
    rig.sm.ptt_start()
    await rig.drain()
    assert rig.plugin._volume_interface is arriving_view, "the re-resolve replaced the interface"
    assert rig.plugin._audio_device_id is None, \
        "an interface the re-resolve replaced must not inherit the previous identity"
    assert rig.reports[-1].reason == "endpoint_unverified"
    assert rig.config_errors == [], "an endpoint the plugin cannot name is not a configuration error"
    assert arriving_view.writes == [] and rig.speakers.writes == [], \
        "no volume level is written through an endpoint the press cannot name"
    assert rig.plugin.get_health_status()["error"]
    assert rig.sm.speech_enabled is False
