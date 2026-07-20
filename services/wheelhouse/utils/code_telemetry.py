"""Telemetry system for tracking execution of potentially dead code paths.

This module provides a lightweight telemetry system to capture when specific
code paths execute during normal operation. Used to determine if code is actually
used before removing it during refactoring.

Key Features:
  - Records execution events to a dedicated file
  - Shows Windows notification on first execution
  - Captures stack trace for context
  - Thread-safe file writing
  - Minimal performance impact

Typical Usage:
  from utils.code_telemetry import track_execution
  
  # In code you want to monitor:
  track_execution(
      code_path="ui_actions.intelligent_insert_text.ack",
      context={"request_id": request_id, "is_terminal": True}
  )
"""
import json
import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
import threading

logger = logging.getLogger(__name__)

# Thread-safe lock for file writing
_telemetry_lock = threading.Lock()

# Track if we've already notified for this code path
_notified_paths = set()

def track_execution(
    code_path: str,
    context: Optional[Dict[str, Any]] = None,
    telemetry_file: str = "code_execution_telemetry.jsonl"
) -> None:
    """
    Record that a specific code path was executed.
    
    Args:
        code_path: Unique identifier for the code path (e.g., "ui_actions.ack.terminal")
        context: Additional context information (request_id, action type, etc.)
        telemetry_file: Name of file to write to (in workspace root)
    """
    try:
        # Capture execution details
        event = {
            "timestamp": datetime.now().isoformat(),
            "code_path": code_path,
            "context": context or {},
            "stack_trace": traceback.format_stack()[-5:-1]  # Last 4 frames before this call
        }
        
        # Write to telemetry file (JSONL format - one JSON object per line)
        workspace_root = Path(__file__).parent.parent.parent.parent
        telemetry_path = workspace_root / telemetry_file
        
        with _telemetry_lock:
            with open(telemetry_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(event) + '\n')
        
        # Show notification ONCE per code path
        if code_path not in _notified_paths:
            _notified_paths.add(code_path)
            _show_notification(code_path, context)
            
    except Exception as e:
        # Never let telemetry break the application
        logger.error(f"Telemetry tracking failed for '{code_path}': {e}", exc_info=True)


def _show_notification(code_path: str, context: Optional[Dict[str, Any]]) -> None:
    """Show a Windows notification that code path was executed."""
    try:
        from plyer import notification
        
        context_str = ""
        if context:
            # Show first 2 context items
            items = list(context.items())[:2]
            context_str = "\n" + ", ".join(f"{k}={v}" for k, v in items)
        
        notification.notify(
            title="Code Path Executed",
            message=f"Path: {code_path}{context_str}",
            app_name="Wheelhouse Telemetry",
            timeout=10
        )
    except Exception as e:
        logger.debug(f"Could not show notification: {e}")


def get_telemetry_summary(telemetry_file: str = "code_execution_telemetry.jsonl") -> Dict[str, int]:
    """
    Get a summary of which code paths have executed and how many times.
    
    Returns:
        Dictionary mapping code_path -> execution_count
    """
    workspace_root = Path(__file__).parent.parent.parent.parent
    telemetry_path = workspace_root / telemetry_file
    
    if not telemetry_path.exists():
        return {}
    
    summary = {}
    with open(telemetry_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                event = json.loads(line)
                code_path = event.get('code_path', 'unknown')
                summary[code_path] = summary.get(code_path, 0) + 1
    
    return summary
