"""Display discovery module for EDID-based display enumeration.

Uses Windows WMI to enumerate connected displays and extract EDID data
for manufacturer identification. Enables zero-configuration brightness
control by auto-detecting connected hardware.

Key Functions:
  - discover_displays(): Enumerate all connected displays
  - parse_edid_manufacturer(): Extract manufacturer from EDID bytes

Usage:
    >>> from services.wheelhouse.integrations.display_discovery import discover_displays
    >>> displays = await discover_displays()
    >>> for d in displays:
    ...     print(f"{d.manufacturer} {d.model} ({'internal' if d.is_internal else 'external'})")
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# EDID manufacturer codes (3-letter PNP IDs)
# See: https://uefi.org/pnp_id_list
MANUFACTURER_CODES = {
    "SNY": "Sony",
    "LGD": "LG Display",
    "LGE": "LG Electronics",
    "SEC": "Samsung",
    "SAM": "Samsung",
    "DEL": "Dell",
    "ACI": "Asus",
    "AUO": "AU Optronics",
    "BOE": "BOE",
    "CMN": "Chi Mei",
    "LEN": "Lenovo",
    "HWP": "HP",
    "ACR": "Acer",
    "BNQ": "BenQ",
    "VSC": "ViewSonic",
    "PHL": "Philips",
    "AOC": "AOC",
}


@dataclass
class DisplayInfo:
    """Information about a connected display.
    
    Attributes:
        display_id: Unique identifier for this display instance
        manufacturer: Human-readable manufacturer name (e.g., "Sony", "Samsung")
        manufacturer_code: Raw 3-letter PNP ID from EDID
        model: Model name/number from EDID
        is_internal: True for laptop built-in panels
        connection_type: "Internal", "HDMI", "DisplayPort", etc.
        edid_raw: Raw EDID bytes for advanced parsing
    """
    display_id: str
    manufacturer: str
    manufacturer_code: str
    model: str
    is_internal: bool
    connection_type: str
    edid_raw: Optional[bytes] = None


def _parse_edid_manufacturer(edid_bytes: bytes) -> tuple[str, str]:
    """Parse manufacturer code from EDID data.
    
    EDID bytes 8-9 contain the manufacturer ID as compressed ASCII.
    Each letter is 5 bits: A=1, B=2, ..., Z=26.
    
    Args:
        edid_bytes: Raw EDID data (minimum 10 bytes)
        
    Returns:
        Tuple of (manufacturer_name, manufacturer_code)
        Returns ("Unknown", "???") if parsing fails
    """
    try:
        if len(edid_bytes) < 10:
            return ("Unknown", "???")
        
        # Manufacturer ID is in bytes 8-9 (big-endian)
        mfg_id = (edid_bytes[8] << 8) | edid_bytes[9]
        
        # Extract three 5-bit characters
        char1 = ((mfg_id >> 10) & 0x1F) + ord('A') - 1
        char2 = ((mfg_id >> 5) & 0x1F) + ord('A') - 1
        char3 = (mfg_id & 0x1F) + ord('A') - 1
        
        code = chr(char1) + chr(char2) + chr(char3)
        name = MANUFACTURER_CODES.get(code, code)  # Use code as fallback
        
        return (name, code)
    except Exception as e:
        logger.warning(f"Failed to parse EDID manufacturer: {e}")
        return ("Unknown", "???")


def _parse_edid_model(edid_bytes: bytes) -> str:
    """Parse model name from EDID descriptor blocks.
    
    EDID has 4 descriptor blocks starting at byte 54, each 18 bytes.
    Block type 0xFC contains the monitor name as ASCII.
    
    Args:
        edid_bytes: Raw EDID data (minimum 128 bytes for base EDID)
        
    Returns:
        Model name string, or "Unknown" if not found
    """
    try:
        if len(edid_bytes) < 128:
            return "Unknown"
        
        # Check each of the 4 descriptor blocks
        for i in range(4):
            offset = 54 + (i * 18)
            
            # Descriptor type is in bytes 0-3
            # 0x000000FC = Monitor Name descriptor
            if (edid_bytes[offset] == 0 and 
                edid_bytes[offset + 1] == 0 and
                edid_bytes[offset + 3] == 0xFC):
                
                # Name is in bytes 5-17 of the descriptor, null/LF terminated
                name_bytes = edid_bytes[offset + 5:offset + 18]
                name = name_bytes.decode('ascii', errors='replace').strip('\x00\n\r ')
                if name:
                    return name
        
        return "Unknown"
    except Exception as e:
        logger.warning(f"Failed to parse EDID model: {e}")
        return "Unknown"


async def discover_displays() -> list[DisplayInfo]:
    """Enumerate all connected displays using WMI.
    
    Uses WmiMonitorDescriptorMethods to retrieve EDID data and
    WmiMonitorConnectionParams for connection type detection.
    
    Returns:
        List of DisplayInfo objects for each connected display
        
    Note:
        Returns empty list on failure (no exceptions raised)
    """
    def _wmi_discover():
        """Synchronous WMI discovery (runs in thread pool)."""
        displays = []
        
        try:
            import wmi
            c = wmi.WMI(namespace='wmi')
            
            # Get all monitors with EDID data
            for idx, monitor in enumerate(c.WmiMonitorDescriptorMethods()):
                try:
                    # Get EDID using WmiGetMonitorRawEEdidV1Block
                    edid_result = monitor.WmiGetMonitorRawEEdidV1Block(0)
                    edid_bytes = bytes(edid_result[0])
                    
                    manufacturer, mfg_code = _parse_edid_manufacturer(edid_bytes)
                    model = _parse_edid_model(edid_bytes)
                    
                    # Determine if internal panel
                    # Internal panels typically have specific manufacturer codes
                    # or connection info, but most reliable is checking instance path
                    instance_name = monitor.InstanceName.upper() if hasattr(monitor, 'InstanceName') else ""
                    is_internal = "DISPLAY\\LEN" in instance_name or "DISPLAY\\AUO" in instance_name or "DISPLAY\\BOE" in instance_name or "DISPLAY\\CMN" in instance_name
                    
                    # Try to determine connection type
                    connection_type = "Unknown"
                    if is_internal:
                        connection_type = "Internal"
                    elif "HDMI" in instance_name:
                        connection_type = "HDMI"
                    elif "DP" in instance_name:
                        connection_type = "DisplayPort"
                    
                    display_info = DisplayInfo(
                        display_id=f"display_{idx}",
                        manufacturer=manufacturer,
                        manufacturer_code=mfg_code,
                        model=model,
                        is_internal=is_internal,
                        connection_type=connection_type,
                        edid_raw=edid_bytes
                    )
                    displays.append(display_info)
                    logger.debug(f"Discovered display: {manufacturer} {model} ({connection_type})")
                    
                except Exception as e:
                    logger.warning(f"Failed to get EDID for monitor {idx}: {e}")
                    continue
                    
        except ImportError:
            logger.error("WMI module not available - display discovery disabled")
        except Exception as e:
            logger.error(f"Display discovery failed: {e}")
        
        return displays
    
    # Run WMI operations in thread pool (blocking I/O)
    return await asyncio.to_thread(_wmi_discover)


def find_sony_displays(displays: list[DisplayInfo]) -> list[DisplayInfo]:
    """Filter displays to find Sony TVs.
    
    Args:
        displays: List of discovered displays
        
    Returns:
        List of Sony displays (potential Bravia TVs)
    """
    return [d for d in displays if d.manufacturer_code == "SNY"]


def find_internal_panel(displays: list[DisplayInfo]) -> Optional[DisplayInfo]:
    """Find the internal laptop panel if present.
    
    Args:
        displays: List of discovered displays
        
    Returns:
        DisplayInfo for internal panel, or None if not found
    """
    for d in displays:
        if d.is_internal:
            return d
    return None
