"""Tests for VolumeRouter handler.

Tests zero-config volume routing including:
- Internal audio detection (_check_internal_audio pure logic)
- Properties and initial state
- Initialize flow with mocked audio/Sonos
- Singleton factory
- Adversarial: empty device names, missing soco
"""

import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Import with Windows API mocking
# ---------------------------------------------------------------------------

@pytest.fixture
def volume_router():
    """Fresh VolumeRouter instance (not singleton)."""
    from handlers.volume_router import VolumeRouter
    return VolumeRouter()


# ===========================================================================
# _check_internal_audio - pure logic, no mocks needed
# ===========================================================================

class TestCheckInternalAudio:
    """Test internal audio device detection logic."""

    def test_realtek_is_internal(self, volume_router):
        assert volume_router._check_internal_audio("Realtek High Definition Audio") is True

    def test_conexant_is_internal(self, volume_router):
        assert volume_router._check_internal_audio("Conexant SmartAudio HD") is True

    def test_intel_is_internal(self, volume_router):
        assert volume_router._check_internal_audio("Intel Display Audio") is True

    def test_speakers_is_internal(self, volume_router):
        assert volume_router._check_internal_audio("Speakers (Realtek Audio)") is True

    def test_headphones_is_internal(self, volume_router):
        assert volume_router._check_internal_audio("Headphones (Realtek Audio)") is True

    def test_headset_is_internal(self, volume_router):
        assert volume_router._check_internal_audio("Headset (Logitech G Pro)") is True

    def test_built_in_is_internal(self, volume_router):
        assert volume_router._check_internal_audio("Built-in Audio") is True

    def test_sonos_is_external(self, volume_router):
        """Sonos device names should not match internal indicators."""
        assert volume_router._check_internal_audio("SONOS Arc") is False

    def test_hdmi_is_external(self, volume_router):
        """HDMI audio should be external."""
        assert volume_router._check_internal_audio("HDMI Audio Device") is False

    def test_displayport_is_external(self, volume_router):
        assert volume_router._check_internal_audio("DisplayPort Audio") is False

    def test_usb_dac_is_external(self, volume_router):
        assert volume_router._check_internal_audio("FiiO USB DAC K5 Pro") is False

    def test_case_insensitive(self, volume_router):
        """Detection should be case-insensitive."""
        assert volume_router._check_internal_audio("REALTEK HIGH DEFINITION AUDIO") is True

    def test_empty_string_is_not_internal(self, volume_router):
        """Acceptance 2, wh-sonos-soundbar-volume: an empty name (no Windows
        output device, for example while the TV is off) is not internal, so the
        check still looks for the TV speaker."""
        assert volume_router._check_internal_audio("") is False

    def test_c_media_is_internal(self, volume_router):
        assert volume_router._check_internal_audio("C-Media USB Audio") is True

    def test_synaptics_is_internal(self, volume_router):
        assert volume_router._check_internal_audio("Synaptics HD Audio") is True

    def test_cirrus_is_internal(self, volume_router):
        assert volume_router._check_internal_audio("Cirrus Logic CS4208") is True


# ===========================================================================
# Properties - initial state
# ===========================================================================

class TestProperties:
    """Test VolumeRouter property defaults."""

    def test_initial_use_sonos_false(self, volume_router):
        assert volume_router.use_sonos is False

    def test_initial_use_system_volume_true(self, volume_router):
        assert volume_router.use_system_volume is True

    def test_initial_sonos_ip_none(self, volume_router):
        assert volume_router.sonos_ip is None

    def test_initial_sonos_name_none(self, volume_router):
        assert volume_router.sonos_name is None

    def test_initial_audio_device_name_empty(self, volume_router):
        assert volume_router.audio_device_name == ""

    def test_use_sonos_inverse_of_use_system_volume(self, volume_router):
        """use_sonos and use_system_volume are logical inverses."""
        assert volume_router.use_sonos != volume_router.use_system_volume


# ===========================================================================
# Initialize - internal audio detected
# ===========================================================================

class TestInitializeInternalAudio:
    """Test initialize() when internal audio is detected."""

    @pytest.mark.asyncio
    async def test_internal_audio_routes_to_system(self, volume_router):
        """When internal audio detected, route to system volume."""
        with patch.object(volume_router, "_get_audio_device_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "Realtek High Definition Audio"
            mock_config = Mock()
            mock_event_bus = Mock()

            await volume_router.initialize(mock_config, mock_event_bus)

            assert volume_router.use_sonos is False
            assert volume_router.use_system_volume is True
            assert volume_router._initialized is True
            assert volume_router.audio_device_name == "Realtek High Definition Audio"


# ===========================================================================
# Initialize - external audio, Sonos found
# ===========================================================================

class TestInitializeExternalWithSonos:
    """Test initialize() when external audio with Sonos speakers. The speakers
    come from the fake Sonos network defined further down."""

    @pytest.mark.asyncio
    async def test_external_audio_with_sonos_tv_routes_to_sonos(self, volume_router, speakers):
        """External audio + Sonos receiving TV = use Sonos."""
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(Mock(), Mock())

        assert volume_router.use_sonos is True
        assert volume_router.sonos_ip == LIVING_ROOM
        assert volume_router.sonos_name == "Living Room"

    @pytest.mark.asyncio
    async def test_external_audio_no_tv_speaker_routes_to_system(self, volume_router, speakers):
        """External audio, two plain speakers, none on TV audio and none a
        soundbar = no TV speaker = system volume."""
        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(Mock(), Mock())

        assert volume_router.use_sonos is False
        assert volume_router.use_system_volume is True

    @pytest.mark.asyncio
    async def test_external_audio_no_sonos_found_routes_to_system(self, volume_router):
        """External audio but no Sonos speakers = system volume."""
        with _sonos_network(volume_router, {}):
            await volume_router.initialize(Mock(), Mock())

        assert volume_router.use_sonos is False
        assert volume_router.sonos_ip is None


# ===========================================================================
# _find_speakers
# ===========================================================================

class TestFindSpeakers:
    """Test the Sonos network search."""

    @pytest.mark.asyncio
    async def test_soco_import_error_returns_defaults(self, volume_router):
        """Missing soco library returns safe defaults."""
        with patch.dict("sys.modules", {"soco": None, "soco.discovery": None}):
            # Force reimport to trigger ImportError
            result = await volume_router._find_speakers()
            assert result == {}

    @pytest.mark.asyncio
    async def test_discovery_returns_none_when_no_speakers(self, volume_router):
        """Returns defaults when no speakers found on network."""
        mock_discovery = MagicMock()
        with patch.dict("sys.modules", {"soco": MagicMock(), "soco.discovery": mock_discovery}):
            mock_discovery.discover = Mock(return_value=None)
            with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
                mock_thread.return_value = None
                result = await volume_router._find_speakers()
                assert result == {}

    @pytest.mark.asyncio
    async def test_discovery_exception_returns_defaults(self, volume_router):
        """Network errors during discovery return safe defaults."""
        with patch("asyncio.to_thread", side_effect=Exception("Network timeout")):
            # Need to make sure soco is importable
            mock_soco = MagicMock()
            with patch.dict("sys.modules", {"soco": mock_soco, "soco.discovery": mock_soco.discovery}):
                result = await volume_router._find_speakers()
                assert result == {}


# ===========================================================================
# _get_audio_device_name
# ===========================================================================

class TestGetAudioDeviceName:
    """Test Windows audio device name retrieval."""

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self, volume_router):
        """Exception during COM device query returns empty string."""
        with patch("asyncio.to_thread", side_effect=Exception("COM error")):
            result = await volume_router._get_audio_device_name()
            assert result == ""


# ===========================================================================
# Singleton factory
# ===========================================================================

class TestSingleton:
    """Test get_volume_router singleton factory."""

    def test_get_volume_router_creates_instance(self):
        from handlers.volume_router import get_volume_router, _volume_router
        import handlers.volume_router as vr_module

        # Reset singleton for test
        vr_module._volume_router = None

        router = get_volume_router()
        assert router is not None
        assert isinstance(router, vr_module.VolumeRouter)

        # Cleanup - reset singleton
        vr_module._volume_router = None

    def test_get_volume_router_returns_same_instance(self):
        import handlers.volume_router as vr_module

        vr_module._volume_router = None

        router1 = vr_module.get_volume_router()
        router2 = vr_module.get_volume_router()
        assert router1 is router2

        # Cleanup
        vr_module._volume_router = None


# ===========================================================================
# Adversarial
# ===========================================================================

class TestAdversarial:
    """Adversarial tests for edge cases."""

    def test_check_internal_audio_with_special_chars(self, volume_router):
        """Device names with special chars don't crash."""
        assert isinstance(
            volume_router._check_internal_audio("Audio (USB\\Device\\0001)"), bool
        )

    def test_check_internal_audio_with_unicode(self, volume_router):
        """Unicode device names don't crash."""
        assert isinstance(
            volume_router._check_internal_audio("Lautsprecher (Realtek)"), bool
        )

    @pytest.mark.asyncio
    async def test_initialize_with_empty_device_name_and_no_speaker(self, volume_router):
        """An empty device name is not internal (wh-sonos-soundbar-volume), so
        the check searches. With no speaker on the network there is no TV
        speaker, and the answer is System Volume."""
        with _sonos_network(volume_router, {}, device_name="") as network:
            await volume_router.initialize(_DictConfig({}), Mock())

            assert network.discover_calls == 1
            assert volume_router.use_sonos is False
            assert volume_router._initialized is True


# ===========================================================================
# Initialize - Sonos plugin enabled setting (wh-volume-router-sonos-enabled)
# ===========================================================================

class _DictConfig:
    """Config stand-in whose get() returns the default for an absent key,
    as ConfigService.get does."""

    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


_SONOS_ON_CONFIGS = [
    pytest.param({}, id="key-absent"),
    pytest.param({"plugins.sonos.enabled": True}, id="enabled-true"),
]


class TestInitializeSonosPluginSetting:
    """The router reads plugins.sonos.enabled with the plugin registry's
    default (True when the key is absent)."""

    @pytest.mark.asyncio
    # Explicit ids with no space: the mutation gate reads a failed case's name
    # from pytest's short summary, and a space in an id cuts that name short.
    @pytest.mark.parametrize(
        "device",
        [
            pytest.param("HDMI Audio Device", id="external"),
            pytest.param("Realtek High Definition Audio", id="internal"),
        ],
    )
    async def test_sonos_plugin_off_routes_to_system_without_search(
        self, volume_router, speakers, device
    ):
        """Plugin off: System Volume, and the Sonos network search never runs."""
        config = _DictConfig({"plugins.sonos.enabled": False})
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI
        with _sonos_network(volume_router, speakers, device_name=device) as network:
            await volume_router.initialize(config, Mock())

        assert volume_router.use_sonos is False
        assert volume_router.use_system_volume is True
        assert volume_router.sonos_ip is None
        assert volume_router._initialized is True
        assert network.discover_calls == 0
        assert network.scan_calls == []

    @pytest.mark.asyncio
    async def test_sonos_plugin_off_never_calls_soco_discover(self, volume_router, speakers):
        """Plugin off: soco.discovery.discover is not called on any interface."""
        config = _DictConfig({"plugins.sonos.enabled": False})
        with _sonos_network(volume_router, speakers) as network:
            with patch.object(soco.discovery, "discover", return_value=None) as mock_discover:
                await volume_router.initialize(config, Mock())

                mock_discover.assert_not_called()
                assert volume_router.use_system_volume is True
        assert network.discover_calls == 0

    @pytest.mark.asyncio
    async def test_reads_registry_key_and_default(self, volume_router, speakers):
        """The router asks for the same key and default as discover_plugins."""
        config = Mock()
        config.get.return_value = False
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI
        with _sonos_network(volume_router, speakers) as network:
            await volume_router.initialize(config, Mock())

        config.get.assert_any_call("plugins.sonos.enabled", True)
        assert network.discover_calls == 0
        assert network.scan_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("values", _SONOS_ON_CONFIGS)
    @pytest.mark.parametrize(
        "device, kind, expect_sonos, expect_search",
        [
            pytest.param("Realtek High Definition Audio", "tv", False, False, id="internal"),
            pytest.param("HDMI Audio Device", "tv", True, True, id="external-sonos-tv"),
            pytest.param("HDMI Audio Device", "plain", False, True, id="external-sonos-not-tv"),
            pytest.param("HDMI Audio Device", "none", False, True, id="external-no-sonos"),
        ],
    )
    async def test_sonos_plugin_on_routing_unchanged(
        self, volume_router, speakers, values, device, kind, expect_sonos, expect_search
    ):
        """Plugin on (explicit or by default): internal output is System Volume
        with no search; external output searches, and a TV speaker gets the
        wheel."""
        config = _DictConfig(values)
        if kind == "tv":
            speakers[LIVING_ROOM].uri = TV_AUDIO_URI
        elif kind == "none":
            speakers.clear()
        with _sonos_network(volume_router, speakers, device_name=device) as network:
            await volume_router.initialize(config, Mock())

        assert volume_router.use_sonos is expect_sonos
        assert volume_router.use_system_volume is (not expect_sonos)
        assert network.discover_calls == (1 if expect_search else 0)


# ===========================================================================
# wh-sonos-tv-recheck: the router checks again for TV audio while it runs
#
# The shipped router decided Sonos or System Volume once, at startup
# (volume_router.py:91 called only from plugins/registry.py:315). A speaker
# that started TV audio after that start never got the thumb wheel.
# ===========================================================================

import contextlib
import logging

import soco.discovery

from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import VolumeAdjustCommand

TV_AUDIO_URI = "x-sonos-htastream:RINCON_1234567890001400"
RADIO_URI = "x-sonosapi-stream:s12345?sid=254&flags=8224&sn=0"

KITCHEN = "192.168.1.50"
LIVING_ROOM = "192.168.1.100"
EXERCISE = "192.168.1.60"


class _FakeSpeaker:
    """Stand-in for a soco.SoCo speaker whose state the test changes.

    ``online`` False makes every network call raise, as a speaker that is
    switched off does. ``fail_next_direct_query`` makes the next
    get_current_transport_info call raise once, as a lost packet does.
    """

    def __init__(self, ip_address, player_name, uri="", household="HH1",
                 is_soundbar=False):
        self.ip_address = ip_address
        self.player_name = player_name
        self.uri = uri
        self.household = household
        self._is_soundbar = is_soundbar
        self.soundbar_unreadable = False
        self.transport_state = "STOPPED"
        self.online = True
        self.fail_next_direct_query = False
        self.transport_calls = 0
        self.volume = 30

    @property
    def is_soundbar(self):
        """soco reads this from the speaker, so it can fail like any call."""
        self._answer()
        if self.soundbar_unreadable:
            raise OSError(f"{self.player_name} gave no speaker info")
        return self._is_soundbar

    @is_soundbar.setter
    def is_soundbar(self, value):
        self._is_soundbar = value

    def _answer(self):
        if not self.online:
            raise OSError(f"{self.player_name} does not answer")

    def get_current_track_info(self):
        self._answer()
        return {"uri": self.uri}

    def get_current_transport_info(self):
        self.transport_calls += 1
        self._answer()
        if self.fail_next_direct_query:
            self.fail_next_direct_query = False
            raise OSError(f"{self.player_name} lost one packet")
        return {"current_transport_state": self.transport_state}


class _FakeNetwork:
    """The Sonos network as soco reports it.

    discover() answers like soco: only the household of the first speaker that
    answers (``first_household``). scan_network(multi_household=True) answers
    with every household. Both skip speakers that are offline.
    """

    def __init__(self, speakers, device_name):
        self.speakers = speakers
        self.device_name = device_name
        self.first_household = None
        self.discover_calls = 0
        self.scan_calls = []

    def _online(self):
        return [s for s in self.speakers.values() if s.online]

    def discover(self, *args, **kwargs):
        self.discover_calls += 1
        online = self._online()
        if not online:
            return None
        household = self.first_household or online[0].household
        return [s for s in online if s.household == household] or None

    def scan_network(self, *args, **kwargs):
        self.scan_calls.append(kwargs)
        online = self._online()
        if not online:
            return None
        if kwargs.get("multi_household") is True:
            return list(online)
        return [s for s in online if s.household == online[0].household]

    def reset_counts(self):
        self.discover_calls = 0
        self.scan_calls = []


@pytest.fixture
def speakers():
    """Two discovered speakers, neither one playing TV audio."""
    return {
        KITCHEN: _FakeSpeaker(KITCHEN, "Kitchen"),
        LIVING_ROOM: _FakeSpeaker(LIVING_ROOM, "Living Room"),
    }


@contextlib.contextmanager
def _sonos_network(router, speakers, device_name="HDMI Audio Device"):
    """Answer the router's network reads from the test's speaker set.

    Yields the _FakeNetwork. A test sets ``network.device_name`` to change the
    Windows output device name between two checks.
    """
    network = _FakeNetwork(speakers, device_name)

    async def _device_name():
        return network.device_name

    with patch.object(router, "_get_audio_device_name", new=_device_name):
        with patch.object(router, "_get_all_local_ips", return_value=["10.0.0.5"]):
            with patch.object(soco.discovery, "discover", new=network.discover), \
                 patch.object(soco.discovery, "scan_network", new=network.scan_network), \
                 patch.object(soco, "SoCo", side_effect=lambda ip: speakers[ip]):
                yield network


async def _start_volume_plugins(router, bus, speakers):
    """Start both volume plugins against this router, bus and speaker set.

    Returns (sonos_plugin, system_volume_plugin, volume_interface).
    """
    from services.wheelhouse.plugins.sonos_plugin import SonosPlugin
    from services.wheelhouse.plugins import sonos_plugin as sonos_module
    from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
    from services.wheelhouse.plugins import system_volume_plugin as system_module

    config = _DictConfig({})
    interface = Mock()
    interface.GetMasterVolumeLevel = Mock(return_value=-20.0)
    interface.SetMasterVolumeLevel = Mock()
    interface.SetMasterVolumeLevelScalar = Mock()

    sonos = SonosPlugin()
    system = SystemVolumePlugin()

    async def _connect():
        system._volume_interface = interface
        system._device_name = "Test Device"

    with patch.object(sonos_module, "get_volume_router", return_value=router), \
         patch.object(system_module, "get_volume_router", return_value=router), \
         patch.object(sonos_module, "SoCo", side_effect=lambda ip: speakers[ip]), \
         patch.object(system_module.AudioUtilities, "GetSpeakers", Mock()), \
         patch.object(system, "_connect_audio_device", new=AsyncMock(side_effect=_connect)), \
         patch.object(SonosPlugin, "_monitor_playback", new=AsyncMock()):
        await sonos.initialize(config, bus)
        await system.initialize(config, bus)
        await sonos.start()
        await system.start()

    return sonos, system, interface


@contextlib.contextmanager
def _plugin_routing(router, speakers):
    """Keep both volume plugins pointed at this router and speaker set.

    _monitor_playback is held open too. A switch to Sonos starts the playback
    watch, and the real watch would reach the network with a real SoCo object
    in whatever module patch is active when its task first runs.
    """
    from services.wheelhouse.plugins.sonos_plugin import SonosPlugin
    from services.wheelhouse.plugins import sonos_plugin as sonos_module
    from services.wheelhouse.plugins import system_volume_plugin as system_module

    with patch.object(sonos_module, "get_volume_router", return_value=router), \
         patch.object(system_module, "get_volume_router", return_value=router), \
         patch.object(sonos_module, "SoCo", side_effect=lambda ip: speakers[ip]), \
         patch.object(SonosPlugin, "_monitor_playback", new=AsyncMock()):
        yield


class TestTvAudioRecheck:
    """wh-sonos-tv-recheck acceptance items 1 through 7."""

    @pytest.mark.asyncio
    async def test_switch_to_sonos_sets_system_volume_to_half_once(
        self, volume_router, speakers
    ):
        """Acceptance 8(a): TV audio starts after startup, so the thumb wheel
        switches to that speaker and the Windows slider goes to 50."""
        bus = EventBus()
        config = _DictConfig({"plugins.sonos.enabled": True})

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(config, bus)
            assert volume_router.use_system_volume is True

            sonos, system, interface = await _start_volume_plugins(
                volume_router, bus, speakers
            )
            interface.SetMasterVolumeLevelScalar.reset_mock()

            # Living Room starts TV audio while Wheelhouse runs.
            speakers["192.168.1.100"].uri = TV_AUDIO_URI

            with _plugin_routing(volume_router, speakers):
                changed = await volume_router.check_tv_audio_now()

                assert changed is True
                assert volume_router.use_sonos is True
                assert volume_router.sonos_name == "Living Room"
                assert volume_router.sonos_ip == "192.168.1.100"

                # The thumb wheel now reaches the Sonos plugin, and that
                # plugin reaches the speaker that plays TV audio.
                await bus.publish(VolumeAdjustCommand(delta=5))

            assert speakers["192.168.1.100"].volume == 35
            assert speakers["192.168.1.50"].volume == 30
            interface.SetMasterVolumeLevel.assert_not_called()

            # The Windows slider reads 50, set exactly once at the switch.
            assert interface.SetMasterVolumeLevelScalar.call_count == 1
            assert interface.SetMasterVolumeLevelScalar.call_args[0][0] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_sonos_plugin_off_never_switches_and_never_sets_half(
        self, volume_router, speakers
    ):
        """Acceptance 8(b): plugins.sonos.enabled false, so no switch and no
        system volume change."""
        bus = EventBus()
        config = _DictConfig({"plugins.sonos.enabled": False})

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(config, bus)
            sonos, system, interface = await _start_volume_plugins(
                volume_router, bus, speakers
            )
            interface.SetMasterVolumeLevelScalar.reset_mock()

            speakers["192.168.1.100"].uri = TV_AUDIO_URI

            with patch.object(
                soco.discovery, "discover", return_value=list(speakers.values())
            ) as discover:
                changed = await volume_router.check_tv_audio_now()

            assert changed is False
            assert volume_router.use_sonos is False
            assert volume_router.use_system_volume is True
            discover.assert_not_called()
            interface.SetMasterVolumeLevelScalar.assert_not_called()

    @pytest.mark.asyncio
    async def test_internal_output_device_switches_back_and_leaves_volume_alone(
        self, volume_router, speakers
    ):
        """Acceptance 10(d), wh-sonos-soundbar-volume: Windows moves its audio
        to the internal speakers, so the thumb wheel goes back to System
        Volume and the system volume level stays where it is. (Before
        wh-sonos-soundbar-volume the TV audio stopping caused the switch back;
        the speaker's source no longer does.)"""
        bus = EventBus()
        config = _DictConfig({"plugins.sonos.enabled": True})
        speakers["192.168.1.100"].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers) as network:
            await volume_router.initialize(config, bus)
            assert volume_router.use_sonos is True

            sonos, system, interface = await _start_volume_plugins(
                volume_router, bus, speakers
            )
            interface.SetMasterVolumeLevelScalar.reset_mock()

            # Windows audio moves to the laptop speakers.
            network.device_name = "Speakers (Realtek High Definition Audio)"

            with _plugin_routing(volume_router, speakers):
                changed = await volume_router.check_tv_audio_now()

                assert changed is True
                assert volume_router.use_system_volume is True

                await bus.publish(VolumeAdjustCommand(delta=5))

            interface.SetMasterVolumeLevelScalar.assert_not_called()
            interface.SetMasterVolumeLevel.assert_called_once()
            assert speakers["192.168.1.100"].volume == 30

    @pytest.mark.asyncio
    async def test_speech_suppression_watch_follows_the_tv_speaker(
        self, volume_router, speakers
    ):
        """Acceptance 6: after the switch, the speech-suppression watch polls
        the speaker that plays TV audio, not an arbitrary discovered one."""
        from services.wheelhouse.plugins.sonos_plugin import SonosPlugin
        from services.wheelhouse.plugins import sonos_plugin as sonos_module
        from services.wheelhouse.plugins.base import PluginState

        # Captured before _plugin_routing replaces the class attribute, so the
        # real watch runs below while the switch's own watch task is a mock.
        real_monitor = SonosPlugin._monitor_playback

        bus = EventBus()
        config = _DictConfig({"plugins.sonos.enabled": True})

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(config, bus)
            sonos, system, interface = await _start_volume_plugins(
                volume_router, bus, speakers
            )
            speakers["192.168.1.100"].uri = TV_AUDIO_URI

            # plugins.sonos.speaker_ip names the other speaker. The router's
            # choice has to win, and this is the only input that tells the two
            # apart: with no configured address, code that reads the
            # configured address first still answers with the router's.
            sonos._configured_speaker_ip = "192.168.1.50"

            with _plugin_routing(volume_router, speakers):
                await volume_router.check_tv_audio_now()

                assert sonos._current_speaker_ip() == "192.168.1.100"

                # One poll of the real watch, then stop it.
                sonos._state = PluginState.RUNNING
                with patch.object(
                    sonos_module, "SoCo", side_effect=lambda ip: speakers[ip]
                ) as soco_ctor:
                    with patch.object(
                        sonos_module.asyncio,
                        "sleep",
                        new=AsyncMock(side_effect=asyncio.CancelledError),
                    ):
                        with pytest.raises(asyncio.CancelledError):
                            await real_monitor(sonos)

                assert soco_ctor.call_args_list[-1][0][0] == "192.168.1.100"

    @pytest.mark.asyncio
    async def test_each_switch_logs_one_info_line_with_direction_and_name(
        self, volume_router, speakers, caplog
    ):
        """Acceptance 7: one INFO line per switch, naming the direction and the
        speaker."""
        bus = EventBus()
        config = _DictConfig({"plugins.sonos.enabled": True})

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(config, bus)
            speakers["192.168.1.100"].uri = TV_AUDIO_URI

            with caplog.at_level(logging.INFO):
                await volume_router.check_tv_audio_now()
                # The second check finds the same speaker on TV audio. One
                # switch happened, so there is one line, not one per check.
                assert await volume_router.check_tv_audio_now() is False

            switch_lines = [
                r.message for r in caplog.records
                if "Switched thumb-wheel volume" in r.message
            ]
            assert len(switch_lines) == 1
            assert "Living Room" in switch_lines[0]
            assert "Sonos" in switch_lines[0]

    @pytest.mark.asyncio
    async def test_volume_command_does_not_wait_for_the_check(
        self, volume_router, speakers
    ):
        """Acceptance 2: the command goes to the target selected when it
        arrives; it never waits for a Sonos search."""
        bus = EventBus()
        config = _DictConfig({"plugins.sonos.enabled": True})

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(config, bus)
            assert volume_router.use_system_volume is True
            sonos, system, interface = await _start_volume_plugins(
                volume_router, bus, speakers
            )

            started = asyncio.Event()
            release = asyncio.Event()

            async def _slow_check():
                started.set()
                await release.wait()
                return False

            with _plugin_routing(volume_router, speakers):
                with patch.object(
                    volume_router, "check_tv_audio_now", new=AsyncMock(side_effect=_slow_check)
                ):
                    # The timeout is the assertion. A command that waits for
                    # the check never returns here, because the check below
                    # is released only after this line. One second keeps that
                    # failure well inside the suite's 30 second limit, which
                    # would otherwise kill the whole run with no verdict.
                    await asyncio.wait_for(
                        bus.publish(VolumeAdjustCommand(delta=5)), timeout=1.0
                    )

                    # The system volume already moved, although the check has
                    # not finished.
                    interface.SetMasterVolumeLevel.assert_called_once()
                    await asyncio.wait_for(started.wait(), timeout=1.0)
                    release.set()
                    await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_one_check_runs_at_a_time(self, volume_router, speakers):
        """A thumb-wheel turn of many steps starts one search, not one per
        step."""
        bus = EventBus()
        config = _DictConfig({"plugins.sonos.enabled": True})

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(config, bus)

            release = asyncio.Event()
            calls = []

            async def _blocking_discover(*args, **kwargs):
                calls.append(1)
                await release.wait()
                return {}

            with patch.object(
                volume_router,
                "_find_speakers",
                new=AsyncMock(side_effect=_blocking_discover),
            ):
                first = asyncio.create_task(volume_router.check_tv_audio_now())
                await asyncio.sleep(0)
                volume_router.request_tv_recheck()
                volume_router.request_tv_recheck()
                await asyncio.sleep(0)

                assert len(calls) == 1
                release.set()
                await first
                await asyncio.sleep(0)


    @pytest.mark.asyncio
    async def test_two_checks_at_once_run_one_search(self, volume_router, speakers):
        """The guard inside check_tv_audio_now itself.

        test_one_check_runs_at_a_time goes through request_tv_recheck, which
        has its own guard reading the same flag, so it cannot see this one.
        """
        bus = EventBus()
        config = _DictConfig({"plugins.sonos.enabled": True})

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(config, bus)

            release = asyncio.Event()
            calls = []

            async def _blocking_discover(*args, **kwargs):
                calls.append(1)
                await release.wait()
                return {}

            with patch.object(
                volume_router,
                "_find_speakers",
                new=AsyncMock(side_effect=_blocking_discover),
            ):
                first = asyncio.create_task(volume_router.check_tv_audio_now())
                await asyncio.sleep(0)
                # The bound turns a broken guard into a failure instead of a
                # hang. Without the guard the second call enters the blocked
                # search and never returns, and pytest-timeout then kills the
                # whole run, which the mutation gate reads as an error rather
                # than a catch.
                try:
                    second = await asyncio.wait_for(
                        volume_router.check_tv_audio_now(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    release.set()
                    await first
                    pytest.fail(
                        "The second check waited for the search of the first "
                        "check instead of returning at once"
                    )

                assert second is False
                assert len(calls) == 1
                release.set()
                await first


class TestTheRepeatedCheckRuns:
    """Acceptance item 1: the check repeats at a fixed interval.

    The interval and the loop need their own tests. The acceptance tests
    above call check_tv_audio_now directly, so none of them would notice a
    loop that never ran or an interval of an hour.
    """

    def test_the_interval_is_between_30_and_60_seconds(self, volume_router):
        interval = type(volume_router).TV_RECHECK_INTERVAL_SECONDS
        assert 30 <= interval <= 60

    @pytest.mark.asyncio
    async def test_the_loop_waits_the_interval_before_each_check(self, volume_router):
        delays = []
        checks = []

        async def _sleep(seconds):
            delays.append(seconds)
            if len(delays) >= 2:
                raise asyncio.CancelledError

        async def _check():
            checks.append(1)
            return False

        with patch("asyncio.sleep", side_effect=_sleep):
            with patch.object(volume_router, "check_tv_audio_now", new=_check):
                with pytest.raises(asyncio.CancelledError):
                    await volume_router._tv_recheck_loop()

        interval = type(volume_router).TV_RECHECK_INTERVAL_SECONDS
        assert delays == [interval, interval]
        assert len(checks) == 1

    @pytest.mark.asyncio
    async def test_the_loop_starts_and_stops(self, volume_router):
        volume_router._config = _DictConfig({"plugins.sonos.enabled": True})

        with patch.object(volume_router, "check_tv_audio_now", new=AsyncMock(return_value=False)):
            volume_router.start_tv_recheck()
            task = volume_router._recheck_task

            assert task is not None
            assert task.done() is False

            await volume_router.stop_tv_recheck()

        assert volume_router._recheck_task is None
        assert task.done() is True

    @pytest.mark.asyncio
    async def test_the_loop_does_not_start_when_the_sonos_plugin_is_off(self, volume_router):
        """Acceptance item 4: nothing about this feature runs with the plugin
        off, so the repeated network search must not run either."""
        volume_router._config = _DictConfig({"plugins.sonos.enabled": False})

        volume_router.start_tv_recheck()

        assert volume_router._recheck_task is None

    @pytest.mark.asyncio
    async def test_a_second_start_does_not_add_a_second_loop(self, volume_router):
        volume_router._config = _DictConfig({"plugins.sonos.enabled": True})

        with patch.object(volume_router, "check_tv_audio_now", new=AsyncMock(return_value=False)):
            volume_router.start_tv_recheck()
            first = volume_router._recheck_task
            volume_router.start_tv_recheck()

            assert volume_router._recheck_task is first

            await volume_router.stop_tv_recheck()


# ===========================================================================
# wh-sonos-soundbar-volume: the thumb wheel stays on the TV soundbar
#
# Before this change the router switched to System Volume whenever the speaker
# stopped playing TV audio (a radio stream, a music service, a pause), and an
# empty Windows output device name counted as internal. Now the speaker that
# plays the TV's audio keeps the wheel whatever it plays.
# ===========================================================================


def _switch_lines(caplog):
    return [
        r.getMessage() for r in caplog.records
        if "Switched thumb-wheel volume" in r.getMessage()
    ]


def _household_speakers(speakers):
    """The speaker set of a house with a second, one-speaker Sonos household."""
    speakers[EXERCISE] = _FakeSpeaker(EXERCISE, "Exercise", household="EX")
    return speakers


class TestTvSpeakerKeepsTheWheel:
    """wh-sonos-soundbar-volume acceptance items 1 to 5 and 10."""

    @pytest.mark.asyncio
    async def test_a_stream_on_the_tv_speaker_never_switches_the_wheel(
        self, volume_router, speakers, caplog
    ):
        """Acceptance 10(a): TV audio, then a radio stream, PLAYING and then
        STOPPED. The wheel stays on Sonos and the Windows volume is not set."""
        bus = EventBus()
        config = _DictConfig({"plugins.sonos.enabled": True})
        living_room = speakers[LIVING_ROOM]
        living_room.uri = TV_AUDIO_URI
        living_room.is_soundbar = True

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(config, bus)
            assert volume_router.use_sonos is True
            sonos, system, interface = await _start_volume_plugins(
                volume_router, bus, speakers
            )
            interface.SetMasterVolumeLevelScalar.reset_mock()

            with _plugin_routing(volume_router, speakers), caplog.at_level(logging.INFO):
                for uri, state in [
                    (RADIO_URI, "PLAYING"),
                    (RADIO_URI, "STOPPED"),
                    ("", "STOPPED"),
                ]:
                    living_room.uri = uri
                    living_room.transport_state = state

                    assert await volume_router.check_tv_audio_now() is False
                    assert volume_router.use_sonos is True
                    assert volume_router.sonos_ip == LIVING_ROOM

                await bus.publish(VolumeAdjustCommand(delta=5))

        assert living_room.volume == 35
        assert speakers[KITCHEN].volume == 30
        interface.SetMasterVolumeLevel.assert_not_called()
        interface.SetMasterVolumeLevelScalar.assert_not_called()
        assert _switch_lines(caplog) == []

    @pytest.mark.asyncio
    async def test_one_soundbar_and_no_tv_audio_selects_sonos(
        self, volume_router, speakers
    ):
        """Acceptance 10(b): no speaker on TV audio and exactly one soundbar."""
        speakers[LIVING_ROOM].is_soundbar = True

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(_DictConfig({}), Mock())

        assert volume_router.use_sonos is True
        assert volume_router.sonos_ip == LIVING_ROOM
        assert volume_router.sonos_name == "Living Room"

    @pytest.mark.asyncio
    async def test_two_soundbars_and_no_tv_audio_select_system_volume(
        self, volume_router, speakers
    ):
        """Acceptance 10(c): two soundbars, none on TV audio, none known."""
        speakers[LIVING_ROOM].is_soundbar = True
        speakers[KITCHEN].is_soundbar = True

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(_DictConfig({}), EventBus())
            assert volume_router.use_sonos is False

            # Still none a check later: the first soundbar is not picked.
            assert await volume_router.check_tv_audio_now() is False
            assert volume_router.use_sonos is False

    @pytest.mark.asyncio
    async def test_an_internal_device_name_selects_system_volume_without_a_search(
        self, volume_router, speakers
    ):
        """Acceptance 10(d): the name is internal, so System Volume, even with
        a soundbar on TV audio, and no network search runs."""
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI
        speakers[LIVING_ROOM].is_soundbar = True

        with _sonos_network(
            volume_router, speakers, device_name="Speakers (Realtek High Definition Audio)"
        ) as network:
            await volume_router.initialize(_DictConfig({}), Mock())

        assert volume_router.use_sonos is False
        assert network.discover_calls == 0
        assert network.scan_calls == []

    @pytest.mark.asyncio
    async def test_an_empty_device_name_keeps_the_wheel_on_the_tv_speaker(
        self, volume_router, speakers
    ):
        """Acceptance 10(d): the Windows output device disappears (the TV is
        off) while a TV speaker exists. The wheel stays on the TV speaker."""
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers) as network:
            await volume_router.initialize(_DictConfig({}), EventBus())
            assert volume_router.use_sonos is True

            network.device_name = ""

            assert await volume_router.check_tv_audio_now() is False
            assert volume_router.use_sonos is True
            assert volume_router.sonos_ip == LIVING_ROOM

    @pytest.mark.asyncio
    async def test_an_empty_device_name_and_one_soundbar_selects_sonos(
        self, volume_router, speakers
    ):
        """Acceptance 10(d): a start with an empty name and exactly one
        soundbar."""
        speakers[LIVING_ROOM].is_soundbar = True

        with _sonos_network(volume_router, speakers, device_name=""):
            await volume_router.initialize(_DictConfig({}), Mock())

        assert volume_router.use_sonos is True
        assert volume_router.sonos_ip == LIVING_ROOM

    @pytest.mark.asyncio
    async def test_one_missed_check_does_not_switch_and_the_second_does(
        self, volume_router, speakers, caplog
    ):
        """Acceptance 10(e): the TV speaker answers neither the direct query
        nor the search. One check changes nothing, the second consecutive one
        goes to System Volume."""
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(_DictConfig({}), EventBus())
            assert volume_router.use_sonos is True

            speakers[LIVING_ROOM].online = False

            with caplog.at_level(logging.INFO):
                assert await volume_router.check_tv_audio_now() is False
                assert volume_router.use_sonos is True
                assert _switch_lines(caplog) == []

                assert await volume_router.check_tv_audio_now() is True
                assert volume_router.use_sonos is False

        lines = _switch_lines(caplog)
        assert len(lines) == 1
        assert "to System Volume" in lines[0]
        assert "'Living Room'" in lines[0]
        assert "(reason: speaker did not answer twice)" in lines[0]

    @pytest.mark.asyncio
    async def test_an_answer_between_two_missed_checks_resets_the_count(
        self, volume_router, speakers
    ):
        """Acceptance 3: two CONSECUTIVE checks. Miss, answer, miss is no
        switch."""
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(_DictConfig({}), EventBus())

            speakers[LIVING_ROOM].online = False
            assert await volume_router.check_tv_audio_now() is False

            speakers[LIVING_ROOM].online = True
            assert await volume_router.check_tv_audio_now() is False

            speakers[LIVING_ROOM].online = False
            assert await volume_router.check_tv_audio_now() is False
            assert volume_router.use_sonos is True

    @pytest.mark.asyncio
    async def test_the_only_other_soundbar_does_not_replace_a_tv_speaker_that_does_not_answer(
        self, volume_router, speakers, caplog
    ):
        """Acceptance 10(h): the known TV speaker does not answer, and the
        search finds exactly one other soundbar and no speaker on TV audio.
        The TV speaker does not change, the miss counts, and the second such
        check goes to System Volume. When the TV speaker answers again, the
        wheel goes back to it, not to the other soundbar."""
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI
        speakers[LIVING_ROOM].is_soundbar = True
        speakers[KITCHEN].is_soundbar = True

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(_DictConfig({}), EventBus())
            assert volume_router.sonos_ip == LIVING_ROOM

            speakers[LIVING_ROOM].online = False

            with caplog.at_level(logging.INFO):
                assert await volume_router.check_tv_audio_now() is False
                assert volume_router.use_sonos is True
                assert volume_router.sonos_ip == LIVING_ROOM

                assert await volume_router.check_tv_audio_now() is True
                assert volume_router.use_sonos is False

                speakers[LIVING_ROOM].online = True
                assert await volume_router.check_tv_audio_now() is True
                assert volume_router.use_sonos is True
                assert volume_router.sonos_ip == LIVING_ROOM

        lines = _switch_lines(caplog)
        assert len(lines) == 2
        assert "to System Volume" in lines[0]
        assert "(reason: speaker did not answer twice)" in lines[0]
        assert all("'Kitchen'" not in line for line in lines)

    @pytest.mark.asyncio
    async def test_the_search_finds_the_tv_speaker_in_another_household_at_startup(
        self, volume_router, speakers
    ):
        """Acceptance 10(f), startup: the first answer to the search comes from
        'Exercise', alone in its household."""
        _household_speakers(speakers)
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers) as network:
            network.first_household = "EX"

            await volume_router.initialize(_DictConfig({}), Mock())

        assert volume_router.use_sonos is True
        assert volume_router.sonos_ip == LIVING_ROOM
        assert network.scan_calls
        assert all(call.get("multi_household") is True for call in network.scan_calls)

    @pytest.mark.asyncio
    async def test_the_search_finds_the_tv_speaker_in_another_household_after_a_failed_direct_query(
        self, volume_router, speakers
    ):
        """Acceptance 10(f), after a failed direct query: the same
        one-speaker household answers first, and the check still finds the TV
        speaker."""
        _household_speakers(speakers)
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers) as network:
            await volume_router.initialize(_DictConfig({}), EventBus())
            assert volume_router.use_sonos is True

            network.first_household = "EX"
            network.reset_counts()
            speakers[LIVING_ROOM].fail_next_direct_query = True

            assert await volume_router.check_tv_audio_now() is False

        assert volume_router.use_sonos is True
        assert volume_router.sonos_ip == LIVING_ROOM
        assert network.scan_calls
        assert all(call.get("multi_household") is True for call in network.scan_calls)

    @pytest.mark.asyncio
    async def test_the_only_soundbar_is_found_in_another_household(
        self, volume_router, speakers
    ):
        """Acceptance 1(c): the soundbar rule counts every household the check
        found. Here the first answer is 'Exercise' and the soundbar sits in the
        other household."""
        _household_speakers(speakers)
        speakers[LIVING_ROOM].is_soundbar = True

        with _sonos_network(volume_router, speakers) as network:
            network.first_household = "EX"

            await volume_router.initialize(_DictConfig({}), Mock())

        assert volume_router.use_sonos is True
        assert volume_router.sonos_ip == LIVING_ROOM

    @pytest.mark.asyncio
    async def test_a_soundbar_in_each_household_is_two_soundbars(
        self, volume_router, speakers
    ):
        """Acceptance 1(c): 'across every household the check found'. One
        soundbar per household is two soundbars, so no TV speaker exists."""
        _household_speakers(speakers)
        speakers[LIVING_ROOM].is_soundbar = True
        speakers[EXERCISE].is_soundbar = True

        with _sonos_network(volume_router, speakers) as network:
            network.first_household = "EX"

            await volume_router.initialize(_DictConfig({}), Mock())

        assert volume_router.use_sonos is False

    @pytest.mark.asyncio
    async def test_a_failed_direct_query_that_the_search_answers_is_not_a_miss(
        self, volume_router, speakers
    ):
        """Acceptance 1(b): the TV speaker identified earlier is kept when the
        search finds it answering, though it plays a stream and another
        soundbar is visible. That check does not count as a miss, so one later
        check with no answer still does not switch."""
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI
        speakers[LIVING_ROOM].is_soundbar = True
        speakers[KITCHEN].is_soundbar = True

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(_DictConfig({}), EventBus())
            assert volume_router.sonos_ip == LIVING_ROOM

            speakers[LIVING_ROOM].uri = RADIO_URI
            speakers[LIVING_ROOM].fail_next_direct_query = True
            assert await volume_router.check_tv_audio_now() is False
            assert volume_router.use_sonos is True
            assert volume_router.sonos_ip == LIVING_ROOM

            # Second check: the TV speaker is gone and no other speaker can
            # take its place (Kitchen stops being a soundbar, so rule (c)
            # finds nothing). This is the first miss, not the second, because
            # the check above was answered.
            speakers[KITCHEN].is_soundbar = False
            speakers[LIVING_ROOM].online = False
            assert await volume_router.check_tv_audio_now() is False
            assert volume_router.use_sonos is True
            assert volume_router.sonos_ip == LIVING_ROOM

    @pytest.mark.asyncio
    async def test_a_speaker_on_tv_audio_wins_over_the_only_soundbar(
        self, volume_router, speakers
    ):
        """Acceptance 1(a): the htastream test comes first. Kitchen is the only
        soundbar, Living Room (not a soundbar) plays TV audio."""
        speakers[KITCHEN].is_soundbar = True
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(_DictConfig({}), Mock())

        assert volume_router.use_sonos is True
        assert volume_router.sonos_ip == LIVING_ROOM

    @pytest.mark.asyncio
    async def test_the_first_tv_soundbar_is_kept_while_it_answers(
        self, volume_router, speakers
    ):
        """Acceptance 4 and 9: with two TV soundbars, the router keeps the
        first one it identified. A check queries it directly, finds it
        answering, and runs no search, though the other one now plays TV
        audio."""
        speakers[LIVING_ROOM].is_soundbar = True
        speakers[KITCHEN].is_soundbar = True
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers) as network:
            await volume_router.initialize(_DictConfig({}), EventBus())
            assert volume_router.sonos_ip == LIVING_ROOM

            speakers[LIVING_ROOM].uri = RADIO_URI
            speakers[KITCHEN].uri = TV_AUDIO_URI
            network.reset_counts()

            assert await volume_router.check_tv_audio_now() is False

        assert volume_router.use_sonos is True
        assert volume_router.sonos_ip == LIVING_ROOM
        assert network.discover_calls == 0

    @pytest.mark.asyncio
    async def test_a_known_tv_speaker_that_answers_makes_no_network_search(
        self, volume_router, speakers
    ):
        """Acceptance 10(g): the check queries the known TV speaker directly by
        its address, and runs neither discover nor scan_network."""
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers) as network:
            await volume_router.initialize(_DictConfig({}), EventBus())
            assert network.discover_calls == 1
            assert len(network.scan_calls) == 1

            network.reset_counts()
            speakers[LIVING_ROOM].transport_calls = 0

            assert await volume_router.check_tv_audio_now() is False

        assert network.discover_calls == 0
        assert network.scan_calls == []
        assert speakers[LIVING_ROOM].transport_calls == 1
        assert volume_router.use_sonos is True

    @pytest.mark.asyncio
    async def test_the_search_runs_when_the_direct_query_fails(
        self, volume_router, speakers
    ):
        """Acceptance 4: the network search runs when the direct query fails."""
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers) as network:
            await volume_router.initialize(_DictConfig({}), EventBus())
            network.reset_counts()
            speakers[LIVING_ROOM].fail_next_direct_query = True

            await volume_router.check_tv_audio_now()

        assert network.discover_calls == 1
        assert len(network.scan_calls) == 1

    @pytest.mark.asyncio
    async def test_a_thumb_wheel_check_does_not_search_while_the_tv_speaker_answers(
        self, volume_router, speakers
    ):
        """Acceptance 4 with the check that a wheel request starts: the same
        decision, so the same direct query."""
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers) as network:
            await volume_router.initialize(_DictConfig({}), EventBus())
            network.reset_counts()

            volume_router.request_tv_recheck()
            await volume_router._request_task

        assert network.discover_calls == 0
        assert network.scan_calls == []


class TestTvSpeakerMemory:
    """The TV speaker identified in this run stays known (acceptance item 1(b)
    and the counting of acceptance item 3)."""

    @pytest.mark.asyncio
    async def test_an_answer_found_by_the_search_resets_the_count(
        self, volume_router, speakers
    ):
        """Miss, then a check whose direct query fails but whose search finds
        the speaker, then miss: still no switch."""
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(_DictConfig({}), EventBus())

            speakers[LIVING_ROOM].online = False
            assert await volume_router.check_tv_audio_now() is False

            speakers[LIVING_ROOM].online = True
            speakers[LIVING_ROOM].fail_next_direct_query = True
            assert await volume_router.check_tv_audio_now() is False

            speakers[LIVING_ROOM].online = False
            assert await volume_router.check_tv_audio_now() is False
            assert volume_router.use_sonos is True

    @pytest.mark.asyncio
    async def test_the_tv_speaker_stays_known_while_system_volume_is_selected(
        self, volume_router, speakers, caplog
    ):
        """The output device goes internal and back. The speaker is queried
        directly, no search runs, and the wheel returns to it although it now
        plays a stream."""
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers) as network:
            await volume_router.initialize(_DictConfig({}), EventBus())

            network.device_name = "Speakers (Realtek Audio)"
            assert await volume_router.check_tv_audio_now() is True
            assert volume_router.use_sonos is False

            speakers[LIVING_ROOM].uri = RADIO_URI
            network.device_name = "HDMI Audio Device"
            network.reset_counts()
            with caplog.at_level(logging.INFO):
                assert await volume_router.check_tv_audio_now() is True

        assert volume_router.use_sonos is True
        assert volume_router.sonos_ip == LIVING_ROOM
        assert network.discover_calls == 0
        assert "(reason: TV audio found)" in _switch_lines(caplog)[-1]

    @pytest.mark.asyncio
    async def test_a_speaker_whose_type_cannot_be_read_blocks_the_soundbar_rule(
        self, volume_router, speakers
    ):
        """A speaker whose is_soundbar cannot be read makes the count of
        soundbars unknown, so the other soundbar is not named the only one."""
        speakers[LIVING_ROOM].is_soundbar = True
        speakers[KITCHEN].soundbar_unreadable = True

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(_DictConfig({}), Mock())

        assert volume_router.use_sonos is False

    @pytest.mark.asyncio
    async def test_the_start_logs_the_target_and_the_reason(
        self, volume_router, speakers, caplog
    ):
        """The start is not a switch, but it writes the same facts."""
        with _sonos_network(volume_router, speakers):
            with caplog.at_level(logging.INFO):
                await volume_router.initialize(_DictConfig({}), Mock())

        starts = [r.getMessage() for r in caplog.records if "starts on" in r.getMessage()]
        assert len(starts) == 1
        assert "System Volume" in starts[0]
        assert "(reason: no TV speaker)" in starts[0]
        assert _switch_lines(caplog) == []


class TestSwitchLogReasons:
    """Acceptance 8: each switch writes one INFO line with the direction, the
    speaker name, and the reason."""

    @pytest.mark.asyncio
    async def test_tv_audio_found(self, volume_router, speakers, caplog):
        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(_DictConfig({}), EventBus())
            assert volume_router.use_sonos is False

            speakers[LIVING_ROOM].uri = TV_AUDIO_URI
            with caplog.at_level(logging.INFO):
                assert await volume_router.check_tv_audio_now() is True

        lines = _switch_lines(caplog)
        assert len(lines) == 1
        assert "to Sonos" in lines[0]
        assert "'Living Room'" in lines[0]
        assert "(reason: TV audio found)" in lines[0]

    @pytest.mark.asyncio
    async def test_only_soundbar(self, volume_router, speakers, caplog):
        speakers[LIVING_ROOM].is_soundbar = True

        with _sonos_network(
            volume_router, speakers, device_name="Speakers (Realtek Audio)"
        ) as network:
            await volume_router.initialize(_DictConfig({}), EventBus())
            assert volume_router.use_sonos is False

            network.device_name = "HDMI Audio Device"
            with caplog.at_level(logging.INFO):
                assert await volume_router.check_tv_audio_now() is True

        lines = _switch_lines(caplog)
        assert len(lines) == 1
        assert "to Sonos" in lines[0]
        assert "'Living Room'" in lines[0]
        assert "(reason: only soundbar)" in lines[0]

    @pytest.mark.asyncio
    async def test_output_device_internal(self, volume_router, speakers, caplog):
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI

        with _sonos_network(volume_router, speakers) as network:
            await volume_router.initialize(_DictConfig({}), EventBus())
            assert volume_router.use_sonos is True

            network.device_name = "Speakers (Realtek Audio)"
            with caplog.at_level(logging.INFO):
                assert await volume_router.check_tv_audio_now() is True

        lines = _switch_lines(caplog)
        assert len(lines) == 1
        assert "to System Volume" in lines[0]
        assert "'Living Room'" in lines[0]
        assert "(reason: output device internal)" in lines[0]

    @pytest.mark.asyncio
    async def test_sonos_plugin_disabled(self, volume_router, speakers, caplog):
        speakers[LIVING_ROOM].uri = TV_AUDIO_URI
        values = {"plugins.sonos.enabled": True}

        with _sonos_network(volume_router, speakers):
            await volume_router.initialize(_DictConfig(values), EventBus())
            assert volume_router.use_sonos is True

            values["plugins.sonos.enabled"] = False
            with caplog.at_level(logging.INFO):
                assert await volume_router.check_tv_audio_now() is True

        lines = _switch_lines(caplog)
        assert len(lines) == 1
        assert "to System Volume" in lines[0]
        assert "'Living Room'" in lines[0]
        assert "(reason: Sonos plugin disabled)" in lines[0]
