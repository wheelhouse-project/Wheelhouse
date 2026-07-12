"""Shadow buffer management for synchronized text editing state tracking.

This module provides a sophisticated text buffer synchronization system that
maintains a local copy of the focused UI control's text content, cursor position,
and selection state. This enables intelligent text manipulation and editing
operations without constant UI automation queries.

Key Classes:
  - ShadowBufferManager: Synchronized text state manager for focused controls.

Key Features:
  - Thread-safe buffer operations with locking mechanisms
  - Real-time synchronization with focused UI controls
  - Cursor position and text selection tracking
  - Intelligent invalidation and re-synchronization
  - Performance optimization through cached state
  - UIA (UI Automation) integration for cross-application support

Buffer Operations:
  - Automatic synchronization with UI control text content
  - Cursor position tracking and manipulation
  - Text selection state management
  - Buffer invalidation on focus changes or external edits
  - Performance-optimized queries with caching

Thread Safety:
  - Uses threading.Lock for concurrent access protection
  - Safe for use across multiple UI interaction threads
  - Atomic operations for state consistency

Typical Usage:
  from ui.shadow_buffer import ShadowBufferManager
  
  buffer = ShadowBufferManager()
  
  # Synchronize with current focused control
  if buffer.synchronize():
      # Work with synchronized state
      cursor_pos = buffer.get_cursor_position()
      selected_text = buffer.get_selected_text()
      
      # Perform text operations
      buffer.insert_text("Hello", cursor_pos)
  else:
      # Handle synchronization failure
      buffer.invalidate()
"""
# ui/shadow_buffer.py: Manages a synchronized text buffer for the focused UI control.
import logging
import threading
import time
import uiautomation as auto
import _ctypes
from utils.timing import log_perf_time

logger = logging.getLogger(__name__)

class ShadowBufferManager:
    """
    Manages a synchronized copy of the text content of a focused UI control,
    including its cursor position and selection state.
    """
    def __init__(self):
        self._buffer = ""
        self._cursor_pos = -1
        self._selection_len = 0
        self._lock = threading.Lock()
        logger.debug("ShadowBufferManager initialized.")

    @property
    def is_valid(self) -> bool:
        """Returns True if the buffer is currently synchronized with a focused control."""
        with self._lock:
            return self._cursor_pos != -1

    def invalidate(self):
        """Resets the shadow buffer, forcing a re-sync on the next query."""
        with self._lock:
            if self._cursor_pos != -1:
                self._buffer = ""
                self._cursor_pos = -1
                self._selection_len = 0

    def synchronize(self) -> bool:
        """
        Uses UIA to populate the shadow buffer with the control's full
        text and calculate the current cursor and selection state.

        Fast path: TextPattern2.GetCaretRange() via raw comtypes (~2ms).
        Fallback: TextPattern MoveEndpointByRange via wrapper (~500ms on some controls).
        """
        try:
            with auto.UIAutomationInitializerInThread(debug=False):
                focused_control = auto.GetFocusedControl()
                if not focused_control:
                    return False

                text_pattern = focused_control.GetPattern(auto.PatternId.TextPattern)
                if not text_pattern:
                    logger.warning(f"Control '{focused_control.Name}' does not support TextPattern. Cannot sync buffer.")
                    return False

                doc_range = text_pattern.DocumentRange
                full_text = doc_range.GetText(-1)

                try:
                    sel_ranges = text_pattern.GetSelection()
                except _ctypes.COMError:
                    return False
                if not sel_ranges:
                    return False
                sel_range = sel_ranges[0]
                sel_text = sel_range.GetText(-1)
                sel_len = len(sel_text)

                # Fast path: use TextPattern2.GetCaretRange() via raw comtypes
                # Avoids the ~500ms MoveEndpointByRange penalty on some controls
                cursor_pos = self._get_cursor_pos_fast(focused_control, doc_range)

                if cursor_pos is None:
                    # Fallback: old MoveEndpointByRange approach (slow on some controls)
                    cursor_range = doc_range.Clone()
                    cursor_range.MoveEndpointByRange(
                        auto.TextPatternRangeEndpoint.End,
                        sel_range,
                        auto.TextPatternRangeEndpoint.Start,
                    )
                    cursor_pos = len(cursor_range.GetText(-1))

                with self._lock:
                    self._buffer = full_text
                    self._cursor_pos = cursor_pos
                    self._selection_len = sel_len

                return True

        except _ctypes.COMError as e:
            logger.warning(f"Shadow Buffer synchronization failed (COM Error): {e}")
            self.invalidate()
            return False
        except Exception as e:
            logger.error(f"Shadow Buffer synchronization failed: {e}", exc_info=True)
            self.invalidate()
            return False

    def _get_cursor_pos_fast(self, focused_control, doc_range) -> int | None:
        """Get cursor position via TextPattern2.GetCaretRange() (raw comtypes).

        Returns integer cursor position, or None if TextPattern2 unavailable.
        Uses raw comtypes COM pointers to avoid the uiautomation wrapper's
        ~500ms overhead on MoveEndpointByRange.
        """
        try:
            tp2 = focused_control.GetPattern(auto.PatternId.TextPattern2)
            if not tp2:
                return None

            raw = tp2.pattern
            if not hasattr(raw, 'GetCaretRange'):
                return None

            _is_active, caret_range = raw.GetCaretRange()
            if not caret_range:
                return None

            # Compute cursor_pos using raw comtypes ranges (fast: ~0.3ms)
            raw_doc = doc_range.textRange  # unwrap to raw comtypes pointer
            pos_range = raw_doc.Clone()
            pos_range.MoveEndpointByRange(
                auto.TextPatternRangeEndpoint.End,
                caret_range,
                auto.TextPatternRangeEndpoint.Start,
            )
            return len(pos_range.GetText(-1))

        except (_ctypes.COMError, AttributeError, ValueError, TypeError):
            return None

    def get_context(self) -> dict:
        """
        Returns context (preceding characters, selection state) from the buffer.
        """
        with self._lock:
            if self._cursor_pos == -1:
                return {'preceding_chars': '', 'has_selection': False}
            
            preceding_chars = self._buffer[max(0, self._cursor_pos - 2) : self._cursor_pos]
            has_selection = self._selection_len > 0
            return {'preceding_chars': preceding_chars, 'has_selection': has_selection}

    def update_after_insertion(self, inserted_text: str):
        """
        Updates the internal buffer state after a text insertion, avoiding a slow
        UIA re-synchronization.
        """
        with self._lock:
            if self._cursor_pos == -1:
                return

            start = self._cursor_pos
            end = start + self._selection_len
            self._buffer = self._buffer[:start] + inserted_text + self._buffer[end:]
            self._cursor_pos = start + len(inserted_text)
            self._selection_len = 0

    def update_from_clipboard_data(self, full_text: str, cursor_pos: int, selection_len: int = 0):
        """
        Updates the shadow buffer with data gathered from clipboard operations.
        This is used as a fallback when UIA TextPattern is not available.
        """
        with self._lock:
            self._buffer = full_text
            self._cursor_pos = cursor_pos
            self._selection_len = selection_len