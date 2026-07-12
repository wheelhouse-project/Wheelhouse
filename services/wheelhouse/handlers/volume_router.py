"""Volume routing with zero-config auto-detection.

This module provides intelligent volume routing based on dual detection:
1. Checks if THIS machine's audio is going to internal speakers (Realtek, etc.)
2. If external, checks if Sonos is receiving TV audio (htastream URI)
3. Routes to Sonos ONLY if internal audio not detected AND Sonos receiving TV audio

Key Classes:
  - VolumeRouter: Central router that determines active volume backend

Usage:
  Router is initialized by PluginRegistry before volume plugins start.
  Plugins check router.use_sonos to decide if they should handle volume.
"""

import asyncio
import ctypes
from ctypes import wintypes
import logging
import socket
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.event_bus import EventBus

logger = logging.getLogger(__name__)


class VolumeRouter:
    """Zero-config volume routing with Sonos auto-discovery.
    
    Determines whether volume commands should go to Sonos API or Windows
    system volume using a dual-check approach:
    1. Check if default audio output is internal (Realtek, Intel, etc.)
    2. If external audio, check if Sonos receiving TV audio via htastream
    
    Both checks must indicate external + Sonos TV audio for Sonos routing.
    
    Decision Matrix:
    - Internal audio detected -> System Volume
    - External audio + Sonos receiving TV audio -> Sonos API
    - External audio + Sonos not receiving TV -> System Volume
    - No Sonos found -> System Volume
    """
    
    # Keywords indicating internal/onboard audio (use System Volume)
    INTERNAL_AUDIO_INDICATORS = [
        "realtek", "conexant", "synaptics", "cirrus", "via", "c-media",
        "intel", "speakers", "headphones", "headset",
        "built-in", "internal"
    ]
    
    def __init__(self):
        self._use_sonos: bool = False
        self._sonos_ip: Optional[str] = None
        self._sonos_name: Optional[str] = None
        self._audio_device_name: str = ""
        self._is_internal_audio: bool = True
        self._initialized: bool = False
    
    @property
    def use_sonos(self) -> bool:
        """True if volume should route to Sonos API."""
        return self._use_sonos
    
    @property
    def use_system_volume(self) -> bool:
        """True if volume should route to Windows system volume."""
        return not self._use_sonos
    
    @property
    def sonos_ip(self) -> Optional[str]:
        """Discovered Sonos speaker IP address, or None."""
        return self._sonos_ip
    
    @property
    def sonos_name(self) -> Optional[str]:
        """Discovered Sonos speaker name, or None."""
        return self._sonos_name
    
    @property
    def audio_device_name(self) -> str:
        """Current Windows audio output device name."""
        return self._audio_device_name
    
    async def initialize(self, config: "ConfigService", event_bus: "EventBus") -> None:
        """Initialize volume routing detection.
        
        Uses dual detection:
        1. Check if audio is going to internal speakers
        2. If external, check if Sonos receiving TV audio
        
        Both must indicate external + Sonos TV audio to use Sonos API.
        """
        logger.info("VolumeRouter: Starting zero-config detection...")
        
        # Step 1: Get audio device name and check if internal
        self._audio_device_name = await self._get_audio_device_name()
        self._is_internal_audio = self._check_internal_audio(self._audio_device_name)
        
        if self._is_internal_audio:
            self._use_sonos = False
            logger.info(
                f"VolumeRouter: Internal audio detected ('{self._audio_device_name}') "
                f"-> using System Volume"
            )
            self._initialized = True
            return
        
        # Step 2: External audio - check if Sonos receiving TV audio
        logger.info(f"VolumeRouter: External audio ('{self._audio_device_name}'), checking Sonos...")
        self._sonos_ip, self._sonos_name, is_receiving_tv = await self._discover_sonos_with_tv_check()
        
        if not self._sonos_ip:
            self._use_sonos = False
            logger.info("VolumeRouter: No Sonos found -> using System Volume")
            self._initialized = True
            return
        
        # Both checks must pass
        self._use_sonos = is_receiving_tv
        
        if is_receiving_tv:
            logger.info(
                f"VolumeRouter: External audio + Sonos '{self._sonos_name}' receiving TV audio "
                f"-> using Sonos API"
            )
        else:
            logger.info(
                f"VolumeRouter: External audio but Sonos not receiving TV audio "
                f"-> using System Volume"
            )
        
        self._initialized = True
    
    def _check_internal_audio(self, device_name: str) -> bool:
        """Check if device name indicates internal/onboard audio.
        
        Returns:
            True if internal audio (use system volume), False if external
        """
        if not device_name:
            # No device name = assume internal to be safe
            return True
        
        device_lower = device_name.lower()
        return any(ind in device_lower for ind in self.INTERNAL_AUDIO_INDICATORS)
    
    async def _get_audio_device_name(self) -> str:
        """Get Windows default audio render device name using WASAPI.
        
        Uses COM/ctypes to query the Windows Core Audio API for the
        friendly name of the default audio playback device.
        
        Returns:
            Device friendly name or empty string on error
        """
        try:
            def _get_device_name():
                try:
                    # COM constants
                    COINIT_APARTMENTTHREADED = 0x2
                    S_OK = 0
                    
                    # GUID helper
                    class GUID(ctypes.Structure):
                        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
                        def __init__(self, s: str = ""):
                            import uuid
                            if s:
                                u = uuid.UUID(s)
                                super().__init__()
                                self.Data1 = u.time_low
                                self.Data2 = u.time_mid
                                self.Data3 = u.time_hi_version
                                d = u.bytes[8:]
                                for i in range(8):
                                    self.Data4[i] = d[i]
                            else:
                                super().__init__()
                    
                    class PROPERTYKEY(ctypes.Structure):
                        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]
                    
                    PKEY_Device_FriendlyName = PROPERTYKEY(
                        GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14
                    )
                    
                    # Opaque COM interfaces
                    class IMMDeviceEnumerator(ctypes.Structure):
                        _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]
                    class IMMDevice(ctypes.Structure):
                        _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]
                    class IPropertyStore(ctypes.Structure):
                        _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]
                    
                    IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
                    CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
                    
                    def VT(ptr, idx, restype, *args):
                        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *args)(
                            ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value,
                                        ctypes.POINTER(ctypes.c_void_p))[idx]
                        )
                    
                    # Initialize COM
                    ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
                    
                    try:
                        # Create device enumerator
                        pEnum = ctypes.POINTER(IMMDeviceEnumerator)()
                        hr = ctypes.windll.ole32.CoCreateInstance(
                            ctypes.byref(CLSID_MMDeviceEnumerator), None, 1,
                            ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(pEnum)
                        )
                        if hr != S_OK or not pEnum:
                            return ""
                        
                        # Get default render endpoint (eRender=0, eConsole=0)
                        GetDefaultAudioEndpoint = VT(pEnum, 4, ctypes.c_long, 
                            ctypes.c_int, ctypes.c_int, 
                            ctypes.POINTER(ctypes.POINTER(IMMDevice)))
                        
                        pp = ctypes.POINTER(IMMDevice)()
                        hr = GetDefaultAudioEndpoint(pEnum, 0, 0, ctypes.byref(pp))
                        if hr != S_OK or not pp:
                            return ""
                        
                        # Open property store
                        OpenPropertyStore = VT(pp, 4, ctypes.c_long, 
                            wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(IPropertyStore)))
                        
                        store = ctypes.POINTER(IPropertyStore)()
                        if OpenPropertyStore(pp, 0, ctypes.byref(store)) != S_OK or not store:
                            return ""
                        
                        # Get friendly name property
                        GetValue = VT(store, 5, ctypes.c_long, 
                            ctypes.POINTER(PROPERTYKEY), ctypes.c_void_p)
                        
                        class PROPVARIANT(ctypes.Structure):
                            _fields_ = [
                                ("vt", wintypes.USHORT),
                                ("wReserved1", wintypes.USHORT),
                                ("wReserved2", wintypes.USHORT),
                                ("wReserved3", wintypes.USHORT),
                                ("pszVal", ctypes.c_wchar_p)
                            ]
                        
                        pv = PROPVARIANT()
                        if GetValue(store, ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(pv)) == S_OK:
                            if pv.vt == 31 and pv.pszVal:  # VT_LPWSTR
                                return pv.pszVal
                        
                        return ""
                    finally:
                        try:
                            ctypes.windll.ole32.CoUninitialize()
                        except Exception:
                            pass
                            
                except Exception as e:
                    logger.debug(f"WASAPI device name error: {e}")
                    return ""
            
            name = await asyncio.to_thread(_get_device_name)
            logger.debug(f"VolumeRouter: Detected audio device: '{name}'")
            return name
            
        except Exception as e:
            logger.warning(f"VolumeRouter: Audio device detection failed: {e}")
            return ""
    
    def _get_all_local_ips(self) -> list[str]:
        """Get all local IPv4 addresses, excluding loopback.

        Uses two complementary methods so every active interface is represented
        regardless of which one is the current default route.

        Returns:
            List of IPv4 address strings, deduplicated
        """
        ips: set[str] = set()

        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127."):
                    ips.add(ip)
        except Exception:
            pass

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass

        return list(ips)

    async def _discover_sonos_with_tv_check(self) -> tuple[Optional[str], Optional[str], bool]:
        """Discover Sonos speakers and check if any are receiving TV audio.

        Scans on every local network interface so discovery succeeds regardless
        of which interface is the default route (e.g. when a VPN is active).

        Returns:
            Tuple of (ip_address, player_name, is_receiving_tv_audio)
            Returns (None, None, False) if no speakers found
        """
        try:
            import soco.discovery

            local_ips = self._get_all_local_ips()
            logger.debug(f"VolumeRouter: Scanning for Sonos on interfaces: {local_ips}")

            # Discover on every interface in parallel, deduplicate by speaker IP
            async def _scan_interface(iface_ip: str):
                try:
                    return await asyncio.to_thread(
                        soco.discovery.discover, timeout=3, interface_addr=iface_ip
                    )
                except Exception as e:
                    logger.debug(f"VolumeRouter: Sonos scan on {iface_ip} failed: {e}")
                    return None

            results = await asyncio.gather(*[_scan_interface(ip) for ip in local_ips])

            discovered: dict[str, object] = {}
            for found in results:
                if found:
                    for speaker in found:
                        discovered[speaker.ip_address] = speaker

            if not discovered:
                logger.debug("VolumeRouter: No Sonos speakers found on any interface")
                return None, None, False

            # Check each unique speaker for TV audio (htastream URI)
            for speaker in discovered.values():
                try:
                    track_info = await asyncio.to_thread(speaker.get_current_track_info)
                    track_uri = track_info.get("uri", "")

                    # htastream = HDMI-ARC/eARC input from TV
                    if track_uri.startswith("x-sonos-htastream:"):
                        logger.info(
                            f"VolumeRouter: Found Sonos '{speaker.player_name}' "
                            f"@ {speaker.ip_address} receiving TV audio"
                        )
                        return speaker.ip_address, speaker.player_name, True
                except Exception as e:
                    logger.debug(f"VolumeRouter: Error checking speaker {speaker.player_name}: {e}")

            # Found speakers, but none receiving TV audio
            first = list(discovered.values())[0]
            logger.info(
                f"VolumeRouter: Discovered Sonos '{first.player_name}' "
                f"@ {first.ip_address} (not receiving TV audio)"
            )
            return first.ip_address, first.player_name, False

        except ImportError:
            logger.warning("VolumeRouter: soco library not available")
            return None, None, False
        except Exception as e:
            logger.warning(f"VolumeRouter: Sonos discovery failed: {e}")
            return None, None, False


# Singleton instance for use across plugins
_volume_router: Optional[VolumeRouter] = None


def get_volume_router() -> VolumeRouter:
    """Get or create the singleton VolumeRouter instance."""
    global _volume_router
    if _volume_router is None:
        _volume_router = VolumeRouter()
    return _volume_router
