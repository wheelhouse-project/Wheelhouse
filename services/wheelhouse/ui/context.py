"""UI Context detection and encapsulation.

This module handles the "sniffing" of the current UI state to determine
the appropriate insertion strategy.
"""
import logging
import psutil
import uiautomation as auto
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

@dataclass
class UIContext:
    """Snapshot of the current UI state."""
    focused_control: any  # uiautomation control
    is_flutter: bool
    is_terminal: bool
    process_name: str
    class_name: str
    process_id: int = 0

def capture_context() -> UIContext:
    """Capture the current UI context.
    
    Determines the focused control and checks for specific application types
    like Flutter or Windows Terminal.
    
    Returns:
        UIContext object containing the state.
    """
    focused_control = auto.GetFocusedControl()
    is_flutter = False
    is_terminal = False
    process_name = ""
    class_name = ""
    process_id = 0

    if focused_control:
        try:
            # Get basic control info
            class_name = focused_control.ClassName

            # Get process info
            try:
                process_id = focused_control.ProcessId
                proc = psutil.Process(process_id)
                process_name = proc.name().lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            # Flutter Detection
            # Logic: Top-level window ClassName starts with 'FLUTTER'
            try:
                top_level = focused_control.GetTopLevelControl()
                if top_level and top_level.ClassName:
                    top_class = top_level.ClassName.upper()
                    if top_class.startswith('FLUTTER'):
                        is_flutter = True
                        logger.debug(f"Flutter detection: ClassName='{top_class}'")
            except Exception as e:
                logger.debug(f"Flutter detection failed: {e}")

            # Terminal Detection
            # 1. Modern Windows Terminal
            if class_name == 'TermControl' and process_name == 'windowsterminal.exe':
                is_terminal = True
                logger.debug(f"Target is Windows Terminal (class={class_name}, process={process_name})")

            # 2. Legacy Console (Task Scheduler, CMD, or Direct Python execution)
            elif class_name == 'ConsoleWindowClass':
                # This captures:
                # - Task Scheduler launching python.exe directly
                # - Task Scheduler launching cmd.exe
                # - You manually running 'cmd.exe' or 'powershell.exe' (classic)
                is_terminal = True
                logger.debug(f"Target is Legacy Console (class={class_name}, process={process_name})")
            
            # 3. Console Window Host (conhost.exe) - Task Scheduler execution
            elif process_name == 'conhost.exe':
                # When WheelHouse runs from Task Scheduler, the focused control
                # is managed by conhost.exe (Console Window Host) with empty class name
                is_terminal = True
                logger.debug(f"Target is Console Host (class={class_name}, process={process_name})")
            
            # 4. Debug logging for undetected cases
            else:
                logger.debug(f"Not detected as terminal: class={class_name}, process={process_name}")

        except Exception as e:
            logger.error(f"Error capturing UI context: {e}")

    return UIContext(
        focused_control=focused_control,
        is_flutter=is_flutter,
        is_terminal=is_terminal,
        process_name=process_name,
        class_name=class_name,
        process_id=process_id,
    )
