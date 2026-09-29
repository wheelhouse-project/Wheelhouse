"""Each process that uses pyperclip selects the Windows clipboard at start.

Without the start step, the first pyperclip copy() or paste() in a process
runs pyperclip's OS detection, which calls platform.system() and can start a
"cmd ver" child process. On a slow host that start held the Input command
loop for 12.4 s and a spoken command expired before dispatch
(wh-input-first-paste-stall).

These are source-level guard tests: calling the real entry functions would
start the whole application. Deleting the start call from an entry point
makes the matching test fail.
"""
from pathlib import Path

from tests.test_process_priority import _call_name, _entry_path_statements

_SERVICE_DIR = Path(__file__).resolve().parents[1]


def _entry_call_names(source_name: str, function_name: str) -> list:
    return [
        _call_name(stmt)
        for stmt in _entry_path_statements(
            _SERVICE_DIR / source_name, function_name
        )
    ]


def _assigns_call_to(stmt, callee: str) -> bool:
    """True when stmt is ``name = callee(...)``."""
    import ast

    if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
        return False
    fn = stmt.value.func
    return (getattr(fn, "id", None) or getattr(fn, "attr", None)) == callee


class TestEntryPointsSelectWindowsClipboard:
    def test_input_entry_selects_windows_clipboard(self):
        assert "select_windows_clipboard" in _entry_call_names(
            "input_proc.py", "input_process_main"
        )

    def test_input_selection_precedes_ui_handler(self):
        # The command loop and every Input clipboard call start after the
        # UIActionHandler is built, so the selection must come first.
        stmts = _entry_path_statements(
            _SERVICE_DIR / "input_proc.py", "input_process_main"
        )
        select_index = next(
            i
            for i, stmt in enumerate(stmts)
            if _call_name(stmt) == "select_windows_clipboard"
        )
        handler_index = next(
            i
            for i, stmt in enumerate(stmts)
            if _assigns_call_to(stmt, "UIActionHandler")
        )
        assert select_index < handler_index

    def test_logic_entry_selects_windows_clipboard(self):
        assert "select_windows_clipboard" in _entry_call_names(
            "main.py", "start_logic_process"
        )

    def test_gui_entry_selects_windows_clipboard(self):
        assert "select_windows_clipboard" in _entry_call_names(
            "gui.py", "gui_process_target"
        )
