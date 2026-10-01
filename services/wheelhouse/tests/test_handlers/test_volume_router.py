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

    def test_empty_string_assumes_internal(self, volume_router):
        """Empty device name assumes internal (safe default)."""
        assert volume_router._check_internal_audio("") is True

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
# Initialize - external audio, Sonos found receiving TV
# ===========================================================================

class TestInitializeExternalWithSonos:
    """Test initialize() when external audio with Sonos TV audio."""

    @pytest.mark.asyncio
    async def test_external_audio_with_sonos_tv_routes_to_sonos(self, volume_router):
        """External audio + Sonos receiving TV = use Sonos."""
        with patch.object(volume_router, "_get_audio_device_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "HDMI Audio Device"

            with patch.object(
                volume_router, "_discover_sonos_with_tv_check", new_callable=AsyncMock
            ) as mock_sonos:
                mock_sonos.return_value = ("192.168.1.100", "Living Room", True)
                mock_config = Mock()
                mock_event_bus = Mock()

                await volume_router.initialize(mock_config, mock_event_bus)

                assert volume_router.use_sonos is True
                assert volume_router.sonos_ip == "192.168.1.100"
                assert volume_router.sonos_name == "Living Room"

    @pytest.mark.asyncio
    async def test_external_audio_sonos_not_receiving_tv_routes_to_system(self, volume_router):
        """External audio + Sonos NOT receiving TV = system volume."""
        with patch.object(volume_router, "_get_audio_device_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "HDMI Audio Device"

            with patch.object(
                volume_router, "_discover_sonos_with_tv_check", new_callable=AsyncMock
            ) as mock_sonos:
                mock_sonos.return_value = ("192.168.1.100", "Living Room", False)
                mock_config = Mock()
                mock_event_bus = Mock()

                await volume_router.initialize(mock_config, mock_event_bus)

                assert volume_router.use_sonos is False
                assert volume_router.use_system_volume is True

    @pytest.mark.asyncio
    async def test_external_audio_no_sonos_found_routes_to_system(self, volume_router):
        """External audio but no Sonos speakers = system volume."""
        with patch.object(volume_router, "_get_audio_device_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "HDMI Audio Device"

            with patch.object(
                volume_router, "_discover_sonos_with_tv_check", new_callable=AsyncMock
            ) as mock_sonos:
                mock_sonos.return_value = (None, None, False)
                mock_config = Mock()
                mock_event_bus = Mock()

                await volume_router.initialize(mock_config, mock_event_bus)

                assert volume_router.use_sonos is False
                assert volume_router.sonos_ip is None


# ===========================================================================
# _discover_sonos_with_tv_check
# ===========================================================================

class TestDiscoverSonos:
    """Test Sonos discovery and TV audio check."""

    @pytest.mark.asyncio
    async def test_soco_import_error_returns_defaults(self, volume_router):
        """Missing soco library returns safe defaults."""
        with patch.dict("sys.modules", {"soco": None, "soco.discovery": None}):
            # Force reimport to trigger ImportError
            result = await volume_router._discover_sonos_with_tv_check()
            assert result == (None, None, False)

    @pytest.mark.asyncio
    async def test_discovery_returns_none_when_no_speakers(self, volume_router):
        """Returns defaults when no speakers found on network."""
        mock_discovery = MagicMock()
        with patch.dict("sys.modules", {"soco": MagicMock(), "soco.discovery": mock_discovery}):
            mock_discovery.discover = Mock(return_value=None)
            with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
                mock_thread.return_value = None
                result = await volume_router._discover_sonos_with_tv_check()
                assert result == (None, None, False)

    @pytest.mark.asyncio
    async def test_discovery_exception_returns_defaults(self, volume_router):
        """Network errors during discovery return safe defaults."""
        with patch("asyncio.to_thread", side_effect=Exception("Network timeout")):
            # Need to make sure soco is importable
            mock_soco = MagicMock()
            with patch.dict("sys.modules", {"soco": mock_soco, "soco.discovery": mock_soco.discovery}):
                result = await volume_router._discover_sonos_with_tv_check()
                assert result == (None, None, False)


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
    async def test_initialize_with_empty_device_name(self, volume_router):
        """Empty device name from WASAPI defaults to internal audio."""
        with patch.object(volume_router, "_get_audio_device_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = ""
            mock_config = Mock()
            mock_event_bus = Mock()

            await volume_router.initialize(mock_config, mock_event_bus)

            # Empty = assumed internal = system volume
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
    async def test_sonos_plugin_off_routes_to_system_without_search(self, volume_router, device):
        """Plugin off: System Volume, and the Sonos network search never runs."""
        config = _DictConfig({"plugins.sonos.enabled": False})
        with patch.object(volume_router, "_get_audio_device_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = device
            with patch.object(
                volume_router, "_discover_sonos_with_tv_check", new_callable=AsyncMock
            ) as mock_sonos:
                mock_sonos.return_value = ("192.168.1.100", "Living Room", True)

                await volume_router.initialize(config, Mock())

                assert volume_router.use_sonos is False
                assert volume_router.use_system_volume is True
                assert volume_router.sonos_ip is None
                assert volume_router._initialized is True
                mock_sonos.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sonos_plugin_off_never_calls_soco_discover(self, volume_router):
        """Plugin off: soco.discovery.discover is not called on any interface."""
        soco_discovery = pytest.importorskip("soco.discovery")
        config = _DictConfig({"plugins.sonos.enabled": False})
        with patch.object(volume_router, "_get_audio_device_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "HDMI Audio Device"
            with patch.object(volume_router, "_get_all_local_ips", return_value=["10.0.0.5"]):
                with patch.object(soco_discovery, "discover", return_value=None) as mock_discover:
                    await volume_router.initialize(config, Mock())

                    mock_discover.assert_not_called()
                    assert volume_router.use_system_volume is True

    @pytest.mark.asyncio
    async def test_reads_registry_key_and_default(self, volume_router):
        """The router asks for the same key and default as discover_plugins."""
        config = Mock()
        config.get.return_value = False
        with patch.object(volume_router, "_get_audio_device_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "HDMI Audio Device"
            with patch.object(
                volume_router, "_discover_sonos_with_tv_check", new_callable=AsyncMock
            ) as mock_sonos:
                # A real discovery result, so code that wrongly reaches the
                # search runs on to the assertions below instead of failing
                # early on unpacking the mock's default return value.
                mock_sonos.return_value = ("192.168.1.100", "Living Room", True)

                await volume_router.initialize(config, Mock())

                config.get.assert_any_call("plugins.sonos.enabled", True)
                mock_sonos.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("values", _SONOS_ON_CONFIGS)
    @pytest.mark.parametrize(
        "device, discovery, expect_sonos, expect_search",
        [
            pytest.param("Realtek High Definition Audio", None, False, False, id="internal"),
            pytest.param("HDMI Audio Device", ("192.168.1.100", "Living Room", True), True, True,
                         id="external-sonos-tv"),
            pytest.param("HDMI Audio Device", ("192.168.1.100", "Living Room", False), False, True,
                         id="external-sonos-not-tv"),
            pytest.param("HDMI Audio Device", (None, None, False), False, True,
                         id="external-no-sonos"),
        ],
    )
    async def test_sonos_plugin_on_routing_unchanged(
        self, volume_router, values, device, discovery, expect_sonos, expect_search
    ):
        """Plugin on (explicit or by default): the dev 199223f2 decision matrix holds."""
        config = _DictConfig(values)
        with patch.object(volume_router, "_get_audio_device_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = device
            with patch.object(
                volume_router, "_discover_sonos_with_tv_check", new_callable=AsyncMock
            ) as mock_sonos:
                mock_sonos.return_value = discovery

                await volume_router.initialize(config, Mock())

                assert volume_router.use_sonos is expect_sonos
                assert volume_router.use_system_volume is (not expect_sonos)
                assert mock_sonos.await_count == (1 if expect_search else 0)
