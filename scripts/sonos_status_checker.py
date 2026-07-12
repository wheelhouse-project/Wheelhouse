
import logging
import sys
from pathlib import Path

# Add the project root to the Python path to allow for absolute imports
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from services.wheelhouse.integrations.sonos_control import get_player

# --- Configuration ---
# You can change this to the IP address if discovery by name fails.
PLAYER_ID = "Living Room"
# --- End Configuration ---

def check_sonos_status():
    """
    Connects to a Sonos player and prints its detailed status, including
    playback state and the current audio source.
    """
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)

    logger.info(f"Attempting to find Sonos player: '{PLAYER_ID}'...")
    player = get_player(PLAYER_ID)

    if not player:
        logger.error("="*50)
        logger.error(f"Could not find the Sonos player '{PLAYER_ID}'.")
        logger.error("Please ensure the player is online and the PLAYER_ID is correct.")
        logger.error("You can try using the player's IP address instead of its name.")
        logger.error("="*50)
        return

    logger.info(f"Successfully found player: {player.player_name} ({player.ip_address})")
    
    try:
        transport_info = player.get_current_transport_info()
        track_info = player.get_current_track_info()
        
        # Correctly determine if the player is playing
        current_state = transport_info.get('current_transport_state')
        is_playing = current_state == 'PLAYING'
        
        print("\n" + "="*50)
        print(f"SONOS STATUS REPORT FOR: {player.player_name}")
        print("="*50)
        
        print(f"\n[Playback State]")
        print(f"  - Is currently playing: {is_playing}")
        print(f"  - Transport State: '{transport_info.get('current_transport_state')}'")

        print(f"\n[Audio Source Information]")
        print(f"  - Title: '{track_info.get('title')}'")
        print(f"  - Artist: '{track_info.get('artist')}'")
        print(f"  - Album: '{track_info.get('album')}'")
        print(f"  - URI: '{track_info.get('uri')}'")
        
        print("\n" + "="*50)
        
        if track_info.get('uri', '').startswith('x-sonos-ht'):
            print("\n[Analysis]")
            print("This appears to be audio from a TV or HDMI source (like your computer).")
            print("Please save the 'URI' value above.")
        elif 'spotify' in track_info.get('uri', ''):
            print("\n[Analysis]")
            print("This appears to be a Spotify stream.")
        else:
            print("\n[Analysis]")
            print("This is likely a music service or other non-TV source.")

    except Exception as e:
        logger.error(f"An error occurred while getting status from {player.player_name}: {e}", exc_info=True)

if __name__ == "__main__":
    check_sonos_status()
