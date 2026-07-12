"""Logitech HID Page Diagnostic Tool.

Run this on any machine to identify which HID pages your Logitech device reports.
This helps debug why different receivers might behave differently.

Usage:
    uv run python scripts/diagnose_hid_pages.py

Move the thumbwheel up and down - the script will show what pages and usage IDs
are being received.
"""

import time
from typing import Set, List

try:
    import pywinusb.hid as hid
except ImportError:
    print("ERROR: pywinusb not installed. Run: uv pip install pywinusb")
    exit(1)

# Logitech device identifiers
LOGITECH_VID = 0x046D
LOGITECH_PIDS: Set[int] = {0xC52B, 0xC539, 0xB023, 0xC52B, 0xC548}

# Known HID pages for thumbwheel
KNOWN_PAGES = {
    0x1203: "Bolt receiver (USB-A variant)",
    0x1302: "Bolt receiver (USB-C variant)",
    0x1303: "Unifying receiver (legacy)",
    0x0F02: "Direct/Other receiver",
}

# Collection of seen data
seen_pages = set()
seen_combinations = {}
event_count = 0


def raw_data_handler(data: List[int]):
    """Process raw HID report and identify pages/usage IDs."""
    global event_count
    
    arr = bytes(data)
    if len(arr) < 6:
        return
    
    # Parse page and usage_id
    page = arr[1] | (arr[2] << 8)
    usage_id = arr[3] | (arr[4] << 8)
    delta_byte = arr[5] if len(arr) > 5 else 0
    delta = delta_byte - 256 if delta_byte > 127 else delta_byte
    
    # Skip if no movement
    if delta == 0:
        return
    
    # Track what we've seen
    key = (page, usage_id)
    if key not in seen_combinations:
        seen_combinations[key] = {"count": 0, "deltas": []}
    seen_combinations[key]["count"] += 1
    seen_combinations[key]["deltas"].append(delta)
    
    seen_pages.add(page)
    event_count += 1
    
    # Get page name
    page_name = KNOWN_PAGES.get(page, "UNKNOWN - NEW PAGE!")
    
    # Print event
    direction = "UP" if delta < 0 else "DOWN"
    print(f"[{event_count:4d}] Page: {page:#06x} ({page_name}) | Usage: {usage_id:#06x} | Delta: {delta:+4d} ({direction})")


def main():
    print("=" * 70)
    print("LOGITECH HID PAGE DIAGNOSTIC TOOL")
    print("=" * 70)
    print()
    print("This tool identifies which HID pages your Logitech device reports.")
    print("Move the THUMBWHEEL up and down to generate events.")
    print("Press Ctrl+C to stop and see summary.")
    print()
    
    # Find Logitech devices
    all_devices = hid.find_all_hid_devices()
    logitech_devices = [
        dev for dev in all_devices
        if dev.vendor_id == LOGITECH_VID and dev.product_id in LOGITECH_PIDS
    ]
    
    if not logitech_devices:
        print(f"ERROR: No Logitech devices found matching VID={LOGITECH_VID:#06x}")
        print(f"       Looking for PIDs: {', '.join(f'{p:#06x}' for p in LOGITECH_PIDS)}")
        print()
        print("Possible issues:")
        print("  - Mouse not connected or paired")
        print("  - Different Logitech device (check Product ID)")
        print("  - Using Bluetooth instead of USB receiver")
        return
    
    print(f"Found {len(logitech_devices)} Logitech device interface(s):")
    for i, dev in enumerate(logitech_devices):
        print(f"  [{i+1}] {dev.product_name}")
        print(f"      VID: {dev.vendor_id:#06x}, PID: {dev.product_id:#06x}")
        print(f"      Path: {dev.device_path[:60]}...")
    print()
    
    # Open all devices
    opened = []
    for dev in logitech_devices:
        try:
            dev.open(shared=True)
            dev.set_raw_data_handler(raw_data_handler)
            opened.append(dev)
        except Exception as e:
            print(f"  Could not open: {dev.product_name}: {e}")
    
    if not opened:
        print("ERROR: Could not open any Logitech devices")
        return
    
    print(f"Listening on {len(opened)} device(s)...")
    print("-" * 70)
    print()
    
    try:
        while True:
            # Check if devices still connected
            if not any(dev.is_plugged() for dev in opened):
                print("\nAll devices disconnected!")
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n")
    
    # Cleanup
    for dev in opened:
        try:
            dev.set_raw_data_handler(None)
            dev.close()
        except:
            pass
    
    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print()
    
    if not seen_pages:
        print("No thumbwheel events detected!")
        print("Make sure to move the thumbwheel (horizontal wheel on side)")
        return
    
    print(f"Total events: {event_count}")
    print()
    
    print("Pages seen:")
    for page in sorted(seen_pages):
        name = KNOWN_PAGES.get(page, "UNKNOWN")
        status = "[+] SUPPORTED" if page in (0x1302, 0x1303, 0x0F02) else "[!] NOT SUPPORTED"
        print(f"  {page:#06x} - {name} {status}")
    print()
    
    print("Page/Usage combinations:")
    for (page, usage), data in sorted(seen_combinations.items()):
        avg_delta = sum(data["deltas"]) / len(data["deltas"]) if data["deltas"] else 0
        print(f"  Page {page:#06x}, Usage {usage:#06x}: {data['count']} events, avg delta: {avg_delta:+.1f}")
    print()
    
    # Recommendations
    unknown_pages = seen_pages - set(KNOWN_PAGES.keys())
    if unknown_pages:
        print("ATTENTION: Unknown pages detected!")
        print(f"  Pages: {', '.join(f'{p:#06x}' for p in unknown_pages)}")
        print("  These should be added to TARGET_PAGE in hid_listener.py")
    else:
        print("All detected pages are supported by WheelHouse.")


if __name__ == "__main__":
    main()
