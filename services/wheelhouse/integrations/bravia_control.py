"""Sony Bravia TV control integration for brightness and display management.

This module provides a REST API interface to Sony Bravia TVs for remote control
of display settings, particularly brightness adjustment. It integrates with the
WheelHouse plugin architecture to enable dynamic TV brightness control.

Key Classes:
  - BraviaControl: DisplayControl implementation for Sony Bravia TVs via REST API.

Key Features:
  - REST API communication with Sony Bravia TVs
  - Normalized 0-100 brightness range (hardware uses 0-50)
  - PSK (Pre-Shared Key) authentication
  - Async/await pattern for non-blocking network calls
  - Error handling for network connectivity issues
  - Configuration validation and warning system

API Integration:
  - Uses Sony Professional Display REST API endpoints
  - setPictureQualitySettings for brightness control
  - HTTP POST requests with JSON payloads
  - PSK-based authentication headers

Typical Usage:
  from integrations.bravia_control import BraviaControl
  
  bravia = BraviaControl(
      ip_address="192.168.1.100",
      psk="your_psk_key"
  )
  
  # Set brightness (0-100 normalized range)
  await bravia.set_brightness(75)  # Maps to 37/50 hardware
  
  # Get current brightness
  current_brightness = await bravia.get_brightness()  # Returns 0-100
"""
# integrations/bravia_control.py
import asyncio
import json
import logging
import select
import socket
import time
from typing import Optional, Tuple

import requests

from services.wheelhouse.integrations.display_control_base import DisplayControl

logger = logging.getLogger(__name__)

# https://pro-bravia.sony.net/develop/integrate/rest-api/spec/service/video/v1_0/setPictureQualitySettings/index.html

DEFAULT_PSK_PLACEHOLDER = "your_psk_here"  # Default PSK in config.sample.json

# Bravia hardware brightness range (native units)
BRAVIA_MIN_BRIGHTNESS = 0
BRAVIA_MAX_BRIGHTNESS = 50

# SSDP Discovery settings
SSDP_MULTICAST_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_TIMEOUT = 3.0  # seconds


def _get_all_local_ips() -> list[str]:
    """Get all local IPv4 addresses for this machine, excluding loopback.

    Uses two complementary methods and combines the results so that any
    active interface (LAN, WiFi, VPN, etc.) is represented.

    Returns:
        List of IPv4 address strings, deduplicated
    """
    ips: set[str] = set()

    # Method 1: resolve hostname - typically returns all registered IPs
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass

    # Method 2: default-route trick as fallback / supplement
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    return list(ips)


async def discover_bravia_ssdp(timeout: float = SSDP_TIMEOUT) -> Optional[str]:
    """Discover Sony Bravia TV on local network using SSDP.

    Sends M-SEARCH multicast on every local network interface simultaneously
    and listens for Sony device responses.  This ensures discovery works
    regardless of which interface is the default route (e.g. when a VPN
    such as Tailscale is active).

    Args:
        timeout: How long to wait for responses (default 3 seconds)

    Returns:
        IP address string of discovered Bravia TV, or None if not found
    """
    def _ssdp_discover():
        local_ips = _get_all_local_ips()
        logger.debug(f"SSDP: Sending M-SEARCH on interfaces: {local_ips}")

        ssdp_request = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_MULTICAST_ADDR}:{SSDP_PORT}\r\n"
            "MAN: \"ssdp:discover\"\r\n"
            "MX: 3\r\n"
            "ST: ssdp:all\r\n"
            "\r\n"
        ).encode()

        sockets: list[socket.socket] = []
        try:
            for local_ip in local_ips:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                    sock.setsockopt(
                        socket.IPPROTO_IP,
                        socket.IP_MULTICAST_IF,
                        socket.inet_aton(local_ip),
                    )
                    sock.bind((local_ip, 0))
                    sock.setblocking(False)
                    sock.sendto(ssdp_request, (SSDP_MULTICAST_ADDR, SSDP_PORT))
                    sockets.append(sock)
                    logger.debug(f"SSDP: Sent M-SEARCH on {local_ip}")
                except Exception as e:
                    logger.debug(f"SSDP: Skipping interface {local_ip}: {e}")

            if not sockets:
                logger.error("SSDP: No interfaces available for discovery")
                return None

            # Poll all sockets simultaneously until timeout
            deadline = time.time() + timeout
            while time.time() < deadline:
                remaining = deadline - time.time()
                readable, _, _ = select.select(sockets, [], [], min(remaining, 0.25))
                for sock in readable:
                    try:
                        data, addr = sock.recvfrom(4096)
                        response = data.decode("utf-8", errors="replace").lower()
                        if "sony" in response:
                            logger.info(f"SSDP: Found Sony device at {addr[0]}")
                            return addr[0]
                    except Exception as e:
                        logger.debug(f"SSDP: Error receiving: {e}")

        finally:
            for sock in sockets:
                try:
                    sock.close()
                except Exception:
                    pass

        logger.debug("SSDP: No Sony devices found")
        return None

    return await asyncio.to_thread(_ssdp_discover)


async def validate_bravia_api(ip_address: str, psk: str, timeout: float = 3.0) -> bool:
    """Validate that an IP address hosts a working Bravia REST API.
    
    Args:
        ip_address: IP address to validate
        psk: Pre-Shared Key for authentication
        timeout: Request timeout in seconds
        
    Returns:
        True if API is accessible, False otherwise
    """
    def _validate():
        try:
            url = f"http://{ip_address}/sony/system"
            headers = {"X-Auth-PSK": psk, "Content-Type": "application/json"}
            payload = {
                "method": "getSystemInformation",
                "id": 1,
                "params": [],
                "version": "1.0"
            }
            
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            response.raise_for_status()
            
            result = response.json()
            if "result" in result:
                logger.info(f"SSDP: Validated Bravia API at {ip_address}")
                return True
            return False
            
        except Exception as e:
            logger.debug(f"SSDP: Failed to validate Bravia API at {ip_address}: {e}")
            return False
    
    return await asyncio.to_thread(_validate)


class BraviaControl(DisplayControl):
    """DisplayControl implementation for Sony Bravia TVs via REST API.
    
    Implements the normalized 0-100 brightness interface by converting to/from
    the Bravia hardware range of 0-50.
    
    All network operations are async using asyncio.to_thread() to prevent
    blocking the event loop.
    """
    
    def __init__(self, ip_address="", psk=""):
        """
        Initialize the BraviaControl class with the TV's IP address and Pre-Shared Key.

        Parameters:
        ip_address (str): The IP address of the TV. No default; supply it
            from the plugin configuration.
        psk (str): The Pre-Shared Key for authentication with the TV. No
            default; supply it from the plugin configuration.
        """
        self.ip_address = ip_address
        self.psk = psk
        self.base_url = f"http://{self.ip_address}/sony/video"
        self.headers = {"X-Auth-PSK": self.psk, "Content-Type": "application/json"}

        if self.psk == DEFAULT_PSK_PLACEHOLDER or not self.psk.strip():
            logger.warning(
                f"BraviaControl initialized with a placeholder or empty PSK ('{self.psk}') for IP {self.ip_address}. "
                "Ensure a valid PSK is configured for the TV to function."
            )
        logger.info(f"BraviaControl initialized for IP: {self.ip_address}")

    @property
    def brightness_range(self) -> Tuple[int, int]:
        """Return Bravia hardware brightness range (0-50)."""
        return (BRAVIA_MIN_BRIGHTNESS, BRAVIA_MAX_BRIGHTNESS)
    
    def _normalize_to_100(self, hardware_value: int) -> int:
        """Convert hardware brightness (0-50) to normalized (0-100)."""
        return int((hardware_value / BRAVIA_MAX_BRIGHTNESS) * 100)
    
    def _normalize_to_hardware(self, normalized_value: int) -> int:
        """Convert normalized brightness (0-100) to hardware (0-50)."""
        # Clamp to 0-100 first
        clamped = max(0, min(100, normalized_value))
        return int((clamped / 100) * BRAVIA_MAX_BRIGHTNESS)

    async def set_brightness(self, level: int) -> Optional[bool]:
        """
        Set the brightness of the TV (0-100 normalized).

        :flow: Brightness Control Abstraction
        :step: 2a
        :description: Hardware implementation of brightness control via REST API
        :data_in: Normalized brightness level (0-100)
        :data_out: HTTP POST to TV
        :notes: Converts normalized level to hardware range (0-50).

        Parameters:
        level (int): The desired brightness level (0-100), converted to 0-50 for TV.
        
        Returns:
        Optional[bool]: True if successful, False if failed, None if TV offline.
        """
        # Convert normalized 0-100 to hardware 0-50
        hardware_brightness = self._normalize_to_hardware(level)

        payload = {
            "method": "setPictureQualitySettings",
            "id": 12,
            "params": [
                {
                    "settings": [
                        {
                            "target": "brightness",
                            "value": str(hardware_brightness),
                        }
                    ]
                }
            ],
            "version": "1.0",
        }
        
        def _send_request():
            """Blocking network call - will be run in thread pool."""
            try:
                response = requests.post(
                    self.base_url, headers=self.headers, data=json.dumps(payload), timeout=5
                )
                response.raise_for_status()
                logger.info(f"Bravia: Successfully set TV brightness to {level}% (hardware: {hardware_brightness}/50)")
                return True
            except requests.exceptions.RequestException as e:
                logger.error(
                    f"Bravia: Failed to set TV brightness to {level}% (hardware: {hardware_brightness}/50). Error: {e}"
                )
                # Check if it's a connection error (TV offline) vs other error
                if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                    return None  # TV offline
                return False  # Command rejected or other error
        
        # Run blocking network call in thread pool
        return await asyncio.to_thread(_send_request)

    async def get_brightness(self) -> Optional[int]:
        """
        Retrieve the current brightness setting of the TV (0-100 normalized).
        
        Returns:
        Optional[int]: Brightness 0-100, or None if TV offline/error occurs.
        """
        payload = {
            "method": "getPictureQualitySettings",
            "id": 13,
            "params": [{"target": "brightness"}],
            "version": "1.0",
        }
        
        def _send_request():
            """Blocking network call - will be run in thread pool."""
            try:
                response = requests.post(
                    self.base_url, headers=self.headers, data=json.dumps(payload), timeout=5
                )
                response.raise_for_status()
                response_data = response.json()

                settings_list = response_data.get("result", [[]])[0]
                for setting in settings_list:
                    if setting.get("target") == "brightness":
                        brightness_str = setting.get("currentValue")
                        if brightness_str is not None:
                            try:
                                hardware_brightness = int(brightness_str)
                                # Convert hardware 0-50 to normalized 0-100
                                normalized_brightness = self._normalize_to_100(hardware_brightness)
                                logger.debug(
                                    f"Bravia: Retrieved TV brightness: {normalized_brightness}% "
                                    f"(hardware: {hardware_brightness}/50)"
                                )
                                return normalized_brightness
                            except ValueError:
                                logger.error(
                                    f"Bravia: Could not convert brightness value '{brightness_str}' to int."
                                )
                                return None
                logger.warning(
                    "Bravia: 'brightness' target not found in getPictureQualitySettings response."
                )
                return None
            except requests.exceptions.RequestException as e:
                logger.error(f"Bravia: Failed to retrieve TV brightness. Error: {e}")
                # Connection errors mean TV is offline
                if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                    return None
                return None
            except (IndexError, KeyError, ValueError) as e:
                logger.error(f"Bravia: Error parsing TV brightness response data: {e}")
                return None
        
        # Run blocking network call in thread pool
        return await asyncio.to_thread(_send_request)

    async def adjust_brightness(self, delta: int) -> Optional[bool]:
        """
        Adjusts the brightness of the Bravia TV by a given delta (0-100 normalized).

        Uses the default DisplayControl implementation (get → calculate → set).
        This could be overridden for optimization if Bravia supports relative commands.

        Args:
            delta (int): The amount by which to adjust the brightness (0-100 scale).
                         Positive values increase brightness, negative values decrease it.

        Returns:
            Optional[bool]: True if brightness was successfully set or already at limit,
                          False on error, None if TV offline.
        """
        # Use the default implementation from DisplayControl
        return await super().adjust_brightness(delta)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d - %(message)s')

    TEST_BRAVIA_IP = "192.168.1.100"
    TEST_BRAVIA_PSK = DEFAULT_PSK_PLACEHOLDER

    async def test_bravia():
        """Test BraviaControl with normalized 0-100 brightness range."""
        if TEST_BRAVIA_PSK == DEFAULT_PSK_PLACEHOLDER or not TEST_BRAVIA_PSK:
            print(f"Please update TEST_BRAVIA_PSK with your actual TV details to run tests (current is '{TEST_BRAVIA_PSK}').")
            return
        
        bravia = BraviaControl(ip_address=TEST_BRAVIA_IP, psk=TEST_BRAVIA_PSK)

        print("\n--- Testing Brightness (Normalized 0-100 Range) ---")
        print(f"Hardware range: {bravia.brightness_range}")
        
        initial_b = await bravia.get_brightness()
        print(f"Initial TV Brightness: {initial_b}%")

        if initial_b is not None:
            print("\nAttempting to dim by 10% (normalized)...")
            await bravia.adjust_brightness(-10)
            await asyncio.sleep(2)
            current_b = await bravia.get_brightness()
            print(f"Brightness after dimming by 10: {current_b}%")
            
            print("\nAttempting to brighten by 20% (normalized, should cap at 100)...")
            await bravia.adjust_brightness(20)
            await asyncio.sleep(2)
            current_b = await bravia.get_brightness()
            print(f"Brightness after brightening by 20: {current_b}%")

            print("\nAttempting to set brightness to 50% (normalized = 25/50 hardware)...")
            await bravia.set_brightness(50)
            await asyncio.sleep(2)
            current_b = await bravia.get_brightness()
            print(f"Brightness after setting to 50%: {current_b}%")

            print("\nAttempting to set brightness to 0% (min)...")
            await bravia.set_brightness(0)
            await asyncio.sleep(2)
            current_b = await bravia.get_brightness()
            print(f"Brightness after setting to 0%: {current_b}%")

            print("\nAttempting to set brightness to 100% (max = 50/50 hardware)...")
            await bravia.set_brightness(100)
            await asyncio.sleep(2)
            current_b = await bravia.get_brightness()
            print(f"Brightness after setting to 100%: {current_b}%")

            print("\nAttempting to set brightness beyond max (e.g., 150%, should cap at 100)...")
            await bravia.set_brightness(150)
            await asyncio.sleep(2)
            current_b = await bravia.get_brightness()
            print(f"Brightness after attempting to set to 150%: {current_b}%")
        else:
            print("Could not retrieve initial brightness. Further tests skipped.")
    
    # Run the async test
    asyncio.run(test_bravia())