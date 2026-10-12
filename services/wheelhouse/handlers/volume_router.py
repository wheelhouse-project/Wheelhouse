"""Volume routing with zero-config auto-detection.

"The TV speaker" below means the Sonos speaker that plays the TV's audio. A
"check" is one run of the volume-target decision: at startup, every
TV_RECHECK_INTERVAL_SECONDS seconds, and when a thumb-wheel command arrives
while System Volume is selected.

The rule (wh-sonos-soundbar-volume):

1. The thumb wheel goes to the TV speaker when plugins.sonos.enabled is true,
   the Windows default output device name is not internal
   (INTERNAL_AUDIO_INDICATORS), and a TV speaker exists. An empty device name
   (no Windows output device, for example while the TV is off) counts as not
   internal. The TV speaker keeps the wheel whatever it plays: TV audio, a
   music or radio stream, or nothing. A change of its source or transport
   state never causes a switch.
2. Each check first queries the known TV speaker directly by its address. When
   that answers, the check runs no network search. The network search runs only
   when no TV speaker is known or the direct query fails. It finds the speakers
   of every Sonos household on the network, because soco's discover() returns
   only the household of the first speaker that answers.
3. The search identifies the TV speaker in this order: (a) a visible speaker
   whose track URI starts with "x-sonos-htastream:"; (b) the TV speaker
   identified earlier in this run, if it answers; (c) only when no TV speaker
   was identified earlier in this run, the only visible speaker that soco
   reports as a soundbar. With two or more soundbars, none on TV audio, and
   none identified earlier, no TV speaker exists.
4. The wheel goes to System Volume when the plugin is disabled, when the output
   device name is internal, when no TV speaker exists, or after two consecutive
   checks in which the TV speaker answers neither the direct query nor the
   search.
5. At a switch from System Volume to the TV speaker, and only then,
   SystemVolumePlugin sets the Windows volume to 50 percent (deferred during a
   push-to-talk hold). A switch to System Volume does not change the Windows
   volume. A volume command never waits for a check.
6. Each switch writes one INFO line with the direction, the speaker name, and
   the reason.

Before wh-sonos-tv-recheck the decision was made once, so a speaker that
started TV audio after the start never received the thumb wheel; the user had
to restart Wheelhouse while the TV played.

Key Classes:
  - VolumeRouter: Central router that determines active volume backend

Usage:
  Router is initialized by PluginRegistry before volume plugins start, and
  PluginRegistry.start_all starts the repeated check after the plugins run.
  Plugins check router.use_sonos to decide if they should handle volume, on
  every command rather than once at start, and they subscribe to
  VolumeTargetChangedEvent to react to a change. The Sonos plugin reads
  router.sonos_ip, the TV speaker whenever one exists, for its volume commands
  and its speech-suppression watch.
"""

import asyncio
import ctypes
from ctypes import wintypes
import logging
import socket
from typing import Optional, TYPE_CHECKING

from services.wheelhouse.events import VolumeTargetChangedEvent

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.event_bus import EventBus

logger = logging.getLogger(__name__)


class VolumeRouter:
    """Zero-config volume routing with Sonos auto-discovery.

    Decision Matrix (the module docstring has the full rule):
    - Sonos plugin disabled (plugins.sonos.enabled = false) -> System Volume
    - Internal audio device name -> System Volume
    - Otherwise, a TV speaker exists -> Sonos API, whatever that speaker plays
    - Otherwise, or after two consecutive unanswered checks -> System Volume
    """
    
    # Keywords indicating internal/onboard audio (use System Volume)
    INTERNAL_AUDIO_INDICATORS = [
        "realtek", "conexant", "synaptics", "cirrus", "via", "c-media",
        "intel", "speakers", "headphones", "headset",
        "built-in", "internal"
    ]
    
    # How long the router waits between two TV-audio checks. Acceptance item 1
    # of wh-sonos-tv-recheck asks for a fixed interval between 30 and 60
    # seconds. A check with a known TV speaker costs one direct query. A check
    # that needs the network search costs soco discover with a 3 second timeout
    # per local interface, one soco scan_network over every household, and one
    # get_current_track_info call per speaker found.
    TV_RECHECK_INTERVAL_SECONDS = 45

    # The wait for the direct query of the known TV speaker. The Sonos plugin
    # sets soco's request timeout to 2 s connect and 5 s read, so a longer wait
    # means the speaker does not answer.
    DIRECT_QUERY_TIMEOUT_SECONDS = 10

    # Consecutive checks in which the known TV speaker answers neither the
    # direct query nor the search before the wheel goes to System Volume.
    MISSED_CHECKS_BEFORE_SWITCH = 2

    HTASTREAM_URI_PREFIX = "x-sonos-htastream:"

    # The reason strings of the switch log line.
    REASON_TV_AUDIO = "TV audio found"
    REASON_ONLY_SOUNDBAR = "only soundbar"
    REASON_NOT_ANSWERING = "speaker did not answer twice"
    REASON_INTERNAL = "output device internal"
    REASON_NO_TV_SPEAKER = "no TV speaker"
    REASON_PLUGIN_DISABLED = "Sonos plugin disabled"

    def __init__(self):
        self._use_sonos: bool = False
        self._sonos_ip: Optional[str] = None
        self._sonos_name: Optional[str] = None
        self._audio_device_name: str = ""
        self._is_internal_audio: bool = True
        self._initialized: bool = False
        self._config: Optional["ConfigService"] = None
        self._event_bus: Optional["EventBus"] = None
        self._recheck_task: Optional[asyncio.Task] = None
        self._request_task: Optional[asyncio.Task] = None
        # One check at a time. A thumb-wheel turn of twenty steps asks for a
        # check per step, and each check is a network search; without this
        # flag the twenty searches would run together.
        self._check_in_progress: bool = False
        # The TV speaker identified in this run. Never cleared: a switch to
        # System Volume keeps it, so the next check queries it directly and
        # rule 3(b) can find it again.
        self._tv_ip: Optional[str] = None
        self._tv_name: Optional[str] = None
        self._tv_reason: Optional[str] = None
        # Consecutive checks in which the known TV speaker did not answer.
        self._missed_checks: int = 0

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
        
        Runs the first check (_decide_volume_target): the Sonos plugin setting,
        the Windows output device name, then the search for the TV speaker.
        The thumb wheel starts on the TV speaker when one exists and the
        output device is not internal.
        """
        logger.info("VolumeRouter: Starting zero-config detection...")
        self._config = config
        self._event_bus = event_bus

        use_sonos, sonos_ip, sonos_name, reason = await self._decide_volume_target()

        self._use_sonos = use_sonos
        self._sonos_ip = sonos_ip
        self._sonos_name = sonos_name
        self._initialized = True

        # The start is not a switch, so it writes no "Switched" line.
        target = f"Sonos '{sonos_name}'" if use_sonos else "System Volume"
        logger.info(
            f"VolumeRouter: Thumb-wheel volume starts on {target} (reason: {reason})"
        )

    def _sonos_plugin_enabled(self) -> bool:
        """Read plugins.sonos.enabled.

        Same key and default as PluginRegistry.discover_plugins: when the
        Sonos plugin is not loaded, nothing would handle Sonos-routed volume.
        """
        if self._config is None:
            return False
        return bool(self._config.get("plugins.sonos.enabled", True))

    async def _decide_volume_target(
        self,
    ) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
        """Run the decision once and report the answer.

        This is the whole of the startup decision, and the repeated check
        calls the same method, so the two can never disagree. It reads the
        Windows output device name every time, which is what lets a user who
        moves Windows audio from the internal speakers to the Sonos or HDMI
        device get the switch at the next check.

        Order: the plugin setting, the device name, the direct query of the
        known TV speaker, then the network search (see the module docstring).

        Returns:
            Tuple of (use_sonos, speaker_ip, speaker_name, reason). The reason
            is one of the REASON_ strings, or None when a first missed check
            leaves the current target as it is. The speaker fields are None
            when no speaker is known.
        """
        self._audio_device_name = await self._get_audio_device_name()
        self._is_internal_audio = self._check_internal_audio(self._audio_device_name)

        # Neither of these two answers touches _missed_checks: a check that
        # never asks the speaker is not a check in which it failed to answer.
        if not self._sonos_plugin_enabled():
            logger.debug("VolumeRouter: Sonos plugin disabled -> System Volume")
            return False, None, None, self.REASON_PLUGIN_DISABLED

        if self._is_internal_audio:
            logger.debug(
                f"VolumeRouter: Internal audio ('{self._audio_device_name}') "
                f"-> System Volume"
            )
            return False, None, None, self.REASON_INTERNAL

        # The known TV speaker is queried by its address first. When it
        # answers, the check runs no network search.
        if self._tv_ip is not None and await self._tv_speaker_answers():
            self._missed_checks = 0
            return True, self._tv_ip, self._tv_name, self._tv_reason

        found, tv_speaker = await self._search_for_tv_speaker()
        if tv_speaker is not None:
            self._tv_ip, self._tv_name, self._tv_reason = tv_speaker
            self._missed_checks = 0
            return True, self._tv_ip, self._tv_name, self._tv_reason

        if self._tv_ip is not None:
            self._missed_checks += 1
            logger.debug(
                f"VolumeRouter: TV speaker '{self._tv_name}' did not answer "
                f"({self._missed_checks} in a row)"
            )
            if self._missed_checks < self.MISSED_CHECKS_BEFORE_SWITCH:
                return self._use_sonos, self._sonos_ip, self._sonos_name, None
            return False, self._tv_ip, self._tv_name, self.REASON_NOT_ANSWERING

        # No TV speaker exists. The first speaker found stays the Sonos
        # plugin's fallback address, as before wh-sonos-soundbar-volume.
        first_ip: Optional[str] = None
        first_name: Optional[str] = None
        if found:
            first = next(iter(found.values()))
            first_ip = first.ip_address
            first_name = await self._speaker_name(first)
            logger.debug(
                f"VolumeRouter: Discovered Sonos '{first_name}' @ {first_ip}, "
                f"none is the TV speaker"
            )
        return False, first_ip, first_name, self.REASON_NO_TV_SPEAKER

    async def _tv_speaker_answers(self) -> bool:
        """Query the known TV speaker directly by its address.

        soco's Service.cache has default_timeout 0 (soco/services.py), so
        get_current_transport_info always goes to the speaker.
        """
        try:
            import soco

            speaker = soco.SoCo(self._tv_ip)
            await asyncio.wait_for(
                asyncio.to_thread(speaker.get_current_transport_info),
                timeout=self.DIRECT_QUERY_TIMEOUT_SECONDS,
            )
            return True
        except Exception as e:
            logger.debug(f"VolumeRouter: Direct query of {self._tv_ip} failed: {e}")
            return False

    async def check_tv_audio_now(self) -> bool:
        """Run the decision again and act on a change.

        The thumb wheel must follow a TV speaker that appears after
        Wheelhouse started (wh-sonos-tv-recheck). Returns True when the
        selected volume target changed, False when it stayed the same or
        another check was already running.
        """
        if self._check_in_progress:
            return False

        self._check_in_progress = True
        try:
            use_sonos, sonos_ip, sonos_name, reason = await self._decide_volume_target()
            return await self._apply_volume_target(use_sonos, sonos_ip, sonos_name, reason)
        finally:
            self._check_in_progress = False

    async def _apply_volume_target(
        self,
        use_sonos: bool,
        sonos_ip: Optional[str],
        sonos_name: Optional[str],
        reason: Optional[str],
    ) -> bool:
        """Store the new decision, and announce it when it changed."""
        if use_sonos:
            # The TV speaker, which the Sonos plugin must use for volume and
            # for its playback watch (acceptance item 7).
            self._sonos_ip = sonos_ip
            self._sonos_name = sonos_name
        elif self._sonos_ip is None and sonos_ip:
            self._sonos_ip = sonos_ip
            self._sonos_name = sonos_name
        # When no TV speaker exists, a known address stays. Otherwise the
        # arbitrary first speaker of the search would move the playback watch
        # to a different speaker every check.

        if use_sonos == self._use_sonos:
            return False

        self._use_sonos = use_sonos
        direction = "to Sonos" if use_sonos else "to System Volume"
        logger.info(
            f"Switched thumb-wheel volume {direction}: speaker "
            f"'{self._sonos_name}' (reason: {reason})"
        )

        if self._event_bus is not None:
            await self._event_bus.publish(VolumeTargetChangedEvent(
                use_sonos=use_sonos,
                speaker_name=self._sonos_name,
                speaker_ip=self._sonos_ip,
            ))

        return True

    def request_tv_recheck(self) -> None:
        """Ask for a TV-audio check and return at once.

        SystemVolumePlugin calls this when a thumb-wheel volume command
        arrives while System Volume is selected. The command itself must not
        wait for the network search (acceptance item 2), so the check runs as
        a separate task.
        """
        if self._check_in_progress:
            return
        if self._request_task is not None and not self._request_task.done():
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("VolumeRouter: no running loop, TV-audio check not requested")
            return

        # The task reference is kept, because the garbage collector can cancel
        # a task that nothing holds.
        self._request_task = loop.create_task(self._check_tv_audio_and_log())

    async def _check_tv_audio_and_log(self) -> None:
        """Run one check and swallow its errors.

        A failed network search must never end the repeated check or the
        volume command that asked for it.
        """
        try:
            await self.check_tv_audio_now()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"VolumeRouter: TV-audio check failed: {e}")

    def start_tv_recheck(self) -> None:
        """Start the repeated TV-audio check.

        PluginRegistry.start_all calls this after the plugins run, so the
        first switch reaches plugins that already subscribe.
        """
        if not self._sonos_plugin_enabled():
            logger.info("VolumeRouter: Sonos plugin disabled -> no TV-audio re-check")
            return
        if self._recheck_task is not None and not self._recheck_task.done():
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("VolumeRouter: no running loop, TV-audio re-check not started")
            return

        self._recheck_task = loop.create_task(self._tv_recheck_loop())
        logger.info(
            f"VolumeRouter: TV-audio re-check every {self.TV_RECHECK_INTERVAL_SECONDS}s"
        )

    async def stop_tv_recheck(self) -> None:
        """Cancel the repeated TV-audio check and wait for it to end."""
        tasks = [t for t in (self._recheck_task, self._request_task) if t is not None]
        self._recheck_task = None
        self._request_task = None

        for task in tasks:
            if task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"VolumeRouter: TV-audio task ended with {e}")

    async def _tv_recheck_loop(self) -> None:
        """Check for TV audio every TV_RECHECK_INTERVAL_SECONDS seconds."""
        while True:
            await asyncio.sleep(self.TV_RECHECK_INTERVAL_SECONDS)
            await self._check_tv_audio_and_log()

    def _check_internal_audio(self, device_name: str) -> bool:
        """Check if device name indicates internal/onboard audio.
        
        An empty name (no Windows output device, for example while the TV is
        off) is not internal, so the check still looks for the TV speaker
        (wh-sonos-soundbar-volume). INTERNAL_AUDIO_INDICATORS is unchanged.

        Returns:
            True if internal audio (use system volume), False if external
        """
        if not device_name:
            return False

        device_lower = device_name.lower()
        return any(ind in device_lower for ind in self.INTERNAL_AUDIO_INDICATORS)
    
    async def _get_audio_device_name(self) -> str:
        """Get Windows default audio render device name using WASAPI.
        
        Uses COM/ctypes to query the Windows Core Audio API for the
        friendly name of the default audio playback device.
        
        Returns:
            Device friendly name or empty string on error
        """
        # crewcut: every COM failure returns '' here, and '' now counts as not
        # internal, so a failed device query keeps the wheel on the TV speaker
        # (it does not force System Volume). A COM failure can also move the
        # wheel from System Volume to the TV speaker until the next check, and
        # that switch sets the Windows volume to 50 percent.
        # Remove the limit by returning a distinct value on a COM failure and
        # keeping the current target.
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

    async def _find_speakers(self) -> dict[str, object]:
        """Search the network for visible Sonos speakers of every household.

        soco's discover() returns only the household of the first speaker that
        answers (soco/discovery.py), so a speaker alone in its own household,
        such as 'Exercise', can hide the household of the TV speaker. The
        search therefore also runs scan_network(multi_household=True), which
        reads the visible zones of every Sonos address on the local networks.

        discover() runs on every local network interface so discovery succeeds
        regardless of which interface is the default route (e.g. when a VPN is
        active). Results merge by speaker IP address.

        Returns:
            Dict of ip_address -> SoCo speaker, empty when nothing answered or
            the soco library is missing.
        """
        try:
            import soco.discovery
        except ImportError:
            logger.warning("VolumeRouter: soco library not available")
            return {}

        try:
            local_ips = self._get_all_local_ips()
            logger.debug(f"VolumeRouter: Scanning for Sonos on interfaces: {local_ips}")

            async def _scan_interface(iface_ip: str):
                try:
                    return await asyncio.to_thread(
                        soco.discovery.discover, timeout=3, interface_addr=iface_ip
                    )
                except Exception as e:
                    logger.debug(f"VolumeRouter: Sonos scan on {iface_ip} failed: {e}")
                    return None

            async def _scan_households():
                try:
                    return await asyncio.to_thread(
                        soco.discovery.scan_network, multi_household=True
                    )
                except Exception as e:
                    logger.debug(f"VolumeRouter: Sonos household scan failed: {e}")
                    return None

            results = await asyncio.gather(
                *[_scan_interface(ip) for ip in local_ips], _scan_households()
            )

            discovered: dict[str, object] = {}
            for found in results:
                if found:
                    for speaker in found:
                        discovered.setdefault(speaker.ip_address, speaker)

            if not discovered:
                logger.debug("VolumeRouter: No Sonos speakers found on any interface")
            return discovered

        except Exception as e:
            logger.warning(f"VolumeRouter: Sonos discovery failed: {e}")
            return {}

    async def _speaker_name(self, speaker) -> Optional[str]:
        """The player name of a speaker, read off the event loop."""
        try:
            return await asyncio.to_thread(lambda: speaker.player_name)
        except Exception as e:
            logger.debug(f"VolumeRouter: Cannot read the name of {speaker.ip_address}: {e}")
            return None

    async def _search_for_tv_speaker(
        self,
    ) -> tuple[dict[str, object], Optional[tuple[str, Optional[str], str]]]:
        """Run the network search and identify the TV speaker.

        crewcut: with two TV soundbars, the router keeps the first one it
        identifies while that speaker answers the direct query of the next
        check, so a second soundbar that plays TV audio later does not take
        the wheel. Remove the limit by running the htastream test of this
        search on every check, not only when the known speaker does not answer.

        Order (acceptance item 1): (a) a visible speaker whose track URI starts
        with "x-sonos-htastream:"; (b) otherwise the TV speaker identified
        earlier in this run, if it answers; (c) otherwise, and only when no TV
        speaker was identified earlier in this run, the only visible speaker,
        across every household, for which soco reports is_soundbar True. With
        two or more soundbars, none on TV audio, and none identified earlier,
        no TV speaker exists.

        Returns:
            Tuple of (speakers found by ip_address, (ip, name, reason) of the
            TV speaker or None).
        """
        found = await self._find_speakers()
        if not found:
            return found, None

        # Track URI of every speaker that answered. A speaker that does not
        # answer is missing from this dict.
        track_uris: dict[str, str] = {}
        for ip, speaker in found.items():
            try:
                track_info = await asyncio.to_thread(speaker.get_current_track_info)
                track_uris[ip] = str(track_info.get("uri") or "")
            except Exception as e:
                logger.debug(f"VolumeRouter: Error checking speaker {ip}: {e}")

        # (a) htastream = HDMI-ARC/eARC input from the TV
        for ip, uri in track_uris.items():
            if uri.startswith(self.HTASTREAM_URI_PREFIX):
                name = await self._speaker_name(found[ip])
                logger.info(f"VolumeRouter: Found Sonos '{name}' @ {ip} receiving TV audio")
                return found, (ip, name, self.REASON_TV_AUDIO)

        # (b) the TV speaker identified earlier, if it answers
        if self._tv_ip is not None and self._tv_ip in track_uris:
            name = await self._speaker_name(found[self._tv_ip])
            return found, (self._tv_ip, name, self._tv_reason or self.REASON_TV_AUDIO)

        # (c) the only soundbar across every household, and only when no TV
        # speaker was identified earlier in this run. Without that condition a
        # short outage of the known TV speaker lets a second soundbar take its
        # place, and the direct query then keeps the second one.
        # crewcut: a TV soundbar that is really replaced is found only after a
        # Wheelhouse restart, or when it plays TV audio (rule a). Remove the
        # limit by letting rule (c) replace the known TV speaker only after it
        # has missed many consecutive checks, for example ten.
        if self._tv_ip is not None:
            return found, None

        # A speaker whose is_soundbar cannot be read makes the count unknown,
        # so no TV speaker is named from it.
        soundbars: list[str] = []
        unknown = False
        for ip, speaker in found.items():
            try:
                if await asyncio.to_thread(lambda s=speaker: s.is_soundbar):
                    soundbars.append(ip)
            except Exception as e:
                logger.debug(f"VolumeRouter: Cannot read is_soundbar of {ip}: {e}")
                unknown = True

        if len(soundbars) == 1 and not unknown:
            ip = soundbars[0]
            name = await self._speaker_name(found[ip])
            logger.info(f"VolumeRouter: Found Sonos '{name}' @ {ip}, the only soundbar")
            return found, (ip, name, self.REASON_ONLY_SOUNDBAR)

        return found, None


# Singleton instance for use across plugins
_volume_router: Optional[VolumeRouter] = None


def get_volume_router() -> VolumeRouter:
    """Get or create the singleton VolumeRouter instance."""
    global _volume_router
    if _volume_router is None:
        _volume_router = VolumeRouter()
    return _volume_router
