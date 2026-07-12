"""OS-level mocks for E2E speech pipeline tests.

Replaces Windows API calls with recording stubs:
- win_input_sender.press_keys -> records keystrokes
- clipboard operations -> in-memory clipboard
- uiautomation -> configurable mock controls
- capture_context -> returns configurable UIContext
"""
from dataclasses import dataclass, field
from typing import List, Tuple, Any
from unittest.mock import MagicMock


@dataclass
class Recording:
    """Records OS-level effects that would have happened."""
    keystrokes: List[Tuple[tuple, dict]] = field(default_factory=list)
    clipboard_pastes: List[str] = field(default_factory=list)
    clipboard_state: str = ""
    run_programs: List[str] = field(default_factory=list)
    typed_texts: List[str] = field(default_factory=list)
    unicode_sends: List[str] = field(default_factory=list)

    def press_keys(self, *keys):
        """Record a press_keys call."""
        self.keystrokes.append((keys, {}))

    def type_string(self, text):
        """Record a type_string call."""
        self.typed_texts.append(text)

    def type_string_verified(self, text):
        """Record a Unicode SendInput delivery (wh-wxkp).

        Mirrors the delivered text into clipboard_pastes so the large body
        of pre-Unicode e2e assertions ("N pastes with these exact strings")
        keeps describing the observable inserted text regardless of which
        transport delivered it. unicode_sends is the precise per-transport
        view for tests that care HOW the text was delivered.

        Returns the (success, chars_sent, error) triple
        utils.win_input_sender.type_string_verified produces.
        """
        self.unicode_sends.append(text)
        self.clipboard_pastes.append(text)
        return True, len(text), None

    def get_keystroke_keys(self) -> List[tuple]:
        """Get just the key tuples from recorded keystrokes."""
        return [ks[0] for ks in self.keystrokes]

    def clear(self):
        self.keystrokes.clear()
        self.clipboard_pastes.clear()
        self.clipboard_state = ""
        self.run_programs.clear()
        self.typed_texts.clear()
        self.unicode_sends.clear()


def make_mock_context(process_name="notepad.exe", is_flutter=False, is_terminal=False):
    """Create a mock UIContext for testing.

    Returns a UIContext-compatible object without importing uiautomation.
    """
    from services.wheelhouse.ui.context import UIContext
    mock_control = MagicMock()
    mock_control.IsKeyboardFocusable = True
    mock_control.ClassName = "Edit"
    mock_control.Exists.return_value = True
    mock_control.ProcessId = 1234
    return UIContext(
        focused_control=mock_control,
        is_flutter=is_flutter,
        is_terminal=is_terminal,
        process_name=process_name,
        class_name="Edit",
    )
