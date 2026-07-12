"""Sonos speaker system control and integration.

This module provides programmatic control over Sonos speakers using the SoCo
library. It handles device discovery, playback control, volume management,
and status monitoring. The module serves as the interface layer between
WheelHouse's input handling system and the Sonos ecosystem.

Key Functions:
  - get_player: Device discovery and connection management.
  - change_volume_sync: Synchronous volume control for input events.
  - Various playback control functions (play, pause, next, previous).

Key Features:
  - Automatic device discovery by name or IP address
  - Robust error handling for network connectivity issues
  - Volume control with relative and absolute adjustment
  - Playback state monitoring and control
  - Integration with mouse wheel and HID input systems

Discovery Methods:
  - By IP address for direct connection
  - By device name for automatic discovery
  - Network discovery with device enumeration

Typical Usage:
  from integrations.sonos_control import get_player, change_volume_sync
  
  # Get player by name or IP
  player = get_player("Living Room")  # or "192.168.1.100"
  
  if player:
      # Volume control (used by mouse wheel)
      change_volume_sync(player, delta=5)  # Increase by 5
      
      # Playback control
      player.play()
      player.pause()
"""
# integrations/sonos_control.py

import logging
from typing import Optional, Union
import time # Added import for time

from soco import SoCo
import soco.discovery
from soco.exceptions import SoCoException, SoCoUPnPException

# Configure logging for this module
logger = logging.getLogger(__name__)

def get_player(player_name_or_ip: Union[str, int]) -> Optional[SoCo]: # Allow int for type hint robustness
    """
    Get a SoCo player object by its name or IP address.

    Args:
        player_name_or_ip: The name (e.g., 'Living Room') or IP address of the Sonos player.

    Returns:
        A SoCo object if the player is found, otherwise None.
    """
    player: Optional[SoCo] = None
    try:
        # Ensure player_name_or_ip is a string before passing to SoCo or discovery
        identifier_str = str(player_name_or_ip)

        # Simple check if it looks like an IP address
        # A more robust IP validation could be used if needed.
        is_ip_like = all(c in "0123456789." for c in identifier_str) and "." in identifier_str

        if is_ip_like:
            logger.debug(f"Attempting to connect to Sonos player by IP: {identifier_str}")
            player = SoCo(identifier_str)
        else:
            logger.debug(f"Attempting to discover Sonos player by name: {identifier_str}")
            player = soco.discovery.by_name(identifier_str)

        if player:
            logger.debug(f"Successfully connected to Sonos player: {player.player_name} ({player.ip_address})")
        else:
            logger.warning(f"Could not find Sonos player: {identifier_str}")
        return player
    except (SoCoException, SoCoUPnPException, ConnectionRefusedError, TypeError) as e: # Added TypeError for inet_aton
        logger.error(f"Error connecting to Sonos player '{player_name_or_ip}': {e}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred in get_player for '{player_name_or_ip}': {e}", exc_info=True)
        return None

def change_volume_sync(player_name_or_ip: Union[str, int], volume_change: int) -> bool:
    """
    Change the volume of a Sonos player by a relative amount.
    This is a synchronous function.

    Args:
        player_name_or_ip: The name or IP address of the Sonos player.
        volume_change: The amount to change the volume by (e.g., +5 or -5).

    Returns:
        True if volume was changed successfully, False otherwise.
    """
    player = get_player(player_name_or_ip)
    if player:
        try:
            current_volume = player.volume
            new_volume = current_volume + volume_change
            # Sonos volume is 0-100
            new_volume = max(0, min(100, new_volume))
            
            if new_volume == current_volume and volume_change != 0:
                logger.info(f"Sonos volume for {player.player_name} already at limit ({current_volume}) for change {volume_change}.")
                return True # No change needed, but not an error

            player.volume = new_volume
            logger.info(f"Changed Sonos volume for {player.player_name} from {current_volume} to {new_volume} (change: {volume_change})")
            return True
        except (SoCoException, SoCoUPnPException) as e:
            logger.error(f"Error changing volume for Sonos player '{player_name_or_ip}': {e}")
            return False
        except Exception as e:
            logger.error(f"An unexpected error occurred changing volume for '{player_name_or_ip}': {e}", exc_info=True)
            return False
    return False

def get_volume_sync(player_name_or_ip: Union[str, int]) -> Optional[int]:
    """
    Get the current volume of a Sonos player.
    This is a synchronous function.

    Args:
        player_name_or_ip: The name or IP address of the Sonos player.

    Returns:
        The current volume (0-100) or None if an error occurs.
    """
    player = get_player(player_name_or_ip)
    if player:
        try:
            volume = player.volume
            logger.info(f"Current volume for Sonos player {player.player_name}: {volume}")
            return volume
        except (SoCoException, SoCoUPnPException) as e:
            logger.error(f"Error getting volume for Sonos player '{player_name_or_ip}': {e}")
            return None
        except Exception as e:
            logger.error(f"An unexpected error occurred getting volume for '{player_name_or_ip}': {e}", exc_info=True)
            return None
    return None

# Example usage (for direct testing of this module)
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(module)s - %(message)s')
    
    # Replace with your Sonos player's name or IP for testing
    # Ensure your Sonos system is on the same network.
    # TEST_PLAYER_ID = "Living Room" 
    TEST_PLAYER_ID = "192.168.1.100" # Example IP

    if TEST_PLAYER_ID:
        print(f"\n--- Testing Sonos Control for Player: {TEST_PLAYER_ID} ---")
        
        initial_volume = get_volume_sync(TEST_PLAYER_ID)
        print(f"Initial volume: {initial_volume}")

        if initial_volume is not None:
            print("\nAttempting to increase volume by 5...")
            change_volume_sync(TEST_PLAYER_ID, 5)
            time.sleep(1) # Give some time for action
            current_volume = get_volume_sync(TEST_PLAYER_ID)
            print(f"Volume after increasing by 5: {current_volume}")

            print("\nAttempting to decrease volume by 10...")
            change_volume_sync(TEST_PLAYER_ID, -10)
            time.sleep(1)
            current_volume = get_volume_sync(TEST_PLAYER_ID)
            print(f"Volume after decreasing by 10: {current_volume}")
            
            # Set back to initial (or a known good volume)
            # print(f"\nAttempting to set volume back to initial: {initial_volume} (or 20 if initial was None/low)...")
            # target_reset_volume = initial_volume if initial_volume is not None and initial_volume > 5 else 20
            # player = get_player(TEST_PLAYER_ID)
            # if player:
            #     player.volume = target_reset_volume
            #     time.sleep(1)
            #     current_volume = get_volume_sync(TEST_PLAYER_ID)
            #     print(f"Volume after reset: {current_volume}")
        else:
            print(f"Could not retrieve initial volume for {TEST_PLAYER_ID}. Further tests skipped.")
    else:
        print("TEST_PLAYER_ID not set. Please set it to your Sonos player's name or IP to run tests.")

