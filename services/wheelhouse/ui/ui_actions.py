"""Backward compatibility module for old import paths.

This module exists solely to maintain backward compatibility with code that
imports from ui.ui_actions. The actual implementation has been refactored
into multiple focused modules.

Old import (still works):
    from ui.ui_actions import UIActionHandler

New preferred import:
    from ui import UIActionHandler
"""

# Re-export from the new module structure
from .ui_action_handler import UIActionHandler

__all__ = ['UIActionHandler']
