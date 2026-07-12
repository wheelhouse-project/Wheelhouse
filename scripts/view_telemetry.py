"""View and analyze code execution telemetry data.

This script reads the telemetry file and displays:
- Summary of which code paths executed
- Detailed events with timestamps and context
- Most recent executions

Usage:
    python scripts/view_telemetry.py                    # Show summary
    python scripts/view_telemetry.py --detailed         # Show all events
    python scripts/view_telemetry.py --clear            # Clear telemetry file
"""
import json
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

def load_telemetry(telemetry_file: Path):
    """Load all telemetry events from file."""
    if not telemetry_file.exists():
        return []
    
    events = []
    with open(telemetry_file, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"Warning: Could not parse line: {e}")
    return events

def show_summary(events):
    """Show summary of code path executions."""
    if not events:
        print("No telemetry data recorded yet.")
        return
    
    # Count executions per code path
    counts = defaultdict(int)
    first_seen = {}
    last_seen = {}
    
    for event in events:
        code_path = event.get('code_path', 'unknown')
        timestamp = event.get('timestamp', '')
        counts[code_path] += 1
        
        if code_path not in first_seen:
            first_seen[code_path] = timestamp
        last_seen[code_path] = timestamp
    
    print("=" * 80)
    print("CODE EXECUTION TELEMETRY SUMMARY")
    print("=" * 80)
    print(f"\nTotal events: {len(events)}")
    print(f"Unique code paths: {len(counts)}")
    print("\n" + "-" * 80)
    print(f"{'Code Path':<50} {'Count':>8} {'Last Seen':>20}")
    print("-" * 80)
    
    for code_path in sorted(counts.keys()):
        count = counts[code_path]
        last = last_seen[code_path]
        # Parse and format timestamp
        try:
            dt = datetime.fromisoformat(last)
            last_fmt = dt.strftime("%Y-%m-%d %H:%M:%S")
        except:
            last_fmt = last[:19] if len(last) >= 19 else last
        
        print(f"{code_path:<50} {count:>8} {last_fmt:>20}")
    
    print("-" * 80)

def show_detailed(events):
    """Show detailed view of all events."""
    if not events:
        print("No telemetry data recorded yet.")
        return
    
    print("=" * 80)
    print("DETAILED CODE EXECUTION EVENTS")
    print("=" * 80)
    
    for i, event in enumerate(events, 1):
        print(f"\nEvent #{i}:")
        print(f"  Time: {event.get('timestamp', 'N/A')}")
        print(f"  Code Path: {event.get('code_path', 'N/A')}")
        
        context = event.get('context', {})
        if context:
            print(f"  Context:")
            for key, value in context.items():
                print(f"    {key}: {value}")
        
        stack = event.get('stack_trace', [])
        if stack:
            print(f"  Stack Trace (last 4 frames):")
            for frame in stack:
                print(f"    {frame.strip()}")
        
        print("-" * 80)

def clear_telemetry(telemetry_file: Path):
    """Clear the telemetry file."""
    if telemetry_file.exists():
        telemetry_file.unlink()
        print(f"Telemetry file cleared: {telemetry_file}")
    else:
        print("No telemetry file to clear.")

def main():
    parser = argparse.ArgumentParser(description="View code execution telemetry")
    parser.add_argument('--detailed', action='store_true', help='Show detailed event information')
    parser.add_argument('--clear', action='store_true', help='Clear telemetry file')
    parser.add_argument('--file', type=str, default='code_execution_telemetry.jsonl',
                       help='Telemetry file name (default: code_execution_telemetry.jsonl)')
    
    args = parser.parse_args()
    
    # Find workspace root and telemetry file
    script_path = Path(__file__).resolve()
    workspace_root = script_path.parent.parent
    telemetry_file = workspace_root / args.file
    
    if args.clear:
        clear_telemetry(telemetry_file)
        return
    
    events = load_telemetry(telemetry_file)
    
    if args.detailed:
        show_detailed(events)
    else:
        show_summary(events)
    
    if events:
        print(f"\nTelemetry file: {telemetry_file}")
        print(f"Use --detailed to see full event details")
        print(f"Use --clear to clear telemetry data")

if __name__ == '__main__':
    main()
