"""Canonical key names the Input process accepts (wh-pattern-editor-r0.5).

``VALID_KEY_NAMES`` mirrors the keys of ``utils/win_input_sender.py``'s
``VK_CODE_MAP`` -- the map ``press_keys`` looks every chord name up in and
ABORTS the whole chord on any unmapped name. The pattern editor validates
hand-typed key names against this set so a pattern can never save with a
chord the Input process would silently refuse to press.

It is a LITERAL copy, not an import: this module is imported by the GUI
process, and importing the sender module would pull in ctypes.windll and
the whole Win32 input stack. The copy is pinned by a sync test
(tests/test_create_pattern_dialog.py::TestKeyNameValidation::
test_valid_key_names_mirror_runtime_vk_map) asserting
``VALID_KEY_NAMES == set(VK_CODE_MAP)``; if the map gains or loses a name,
that test fails until this set is updated to match.

Stdlib-only on purpose.
"""

VALID_KEY_NAMES = frozenset({
    # Named keys (including the 'del' alias for 'delete')
    "backspace", "tab", "enter", "shift", "ctrl", "alt", "pause",
    "capslock", "esc", "space", "pageup", "pagedown", "end", "home",
    "left", "up", "right", "down", "printscreen", "insert", "delete",
    "del", "win", "lwin",
    # Digits
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
    # Letters
    "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m",
    "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
    # Punctuation (each OEM key's unshifted and shifted spellings)
    "=", "+", "-", "_", ";", ":", "/", "?", "`", "~", "[", "{",
    "\\", "|", "]", "}", "'", '"', ",", "<", ".", ">",
    # Function keys
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10",
    "f11", "f12",
})
