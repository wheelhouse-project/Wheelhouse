"""Action function library for speech command execution.

This module provides a comprehensive library of action functions that can be
called from the speech command parsing system. It includes both UI-bound
functions that generate command payloads for the UI process, and local
functions that execute directly within the speech processing context.

Key Classes:
  - ActionFunctions: Main registry and dispatcher for speech command actions.

Key Functions:
  - UI Actions: hotkey, press, type_text, activate_window, etc.
  - System Actions: search, timestamp, window_screenshot, etc.
  - Utility Functions: words_to_int for numeric parameter processing

Action Categories:
  - Keyboard/Mouse Control: Hotkeys, key presses, mouse actions
  - Window Management: Application activation and window control
  - Text Processing: Text insertion, clipboard operations
  - System Integration: Screenshot capture, search functions
  - Application Launching: Browser, applications, system utilities

Function Registration:
  - Functions are automatically registered in _register_functions
  - UI-bound functions return dictionary payloads for IPC
  - Local functions execute immediately and return None
  - Parameterized functions support regex capture group arguments

Typical Usage:
  from speech.actions import ActionFunctions, words_to_int
  
  actions = ActionFunctions(speech_handler)
  
  # Execute action function
  result = actions.call_function("hotkey", "ctrl+c")
  
  # Process numeric parameters
  count = words_to_int("three")  # Returns: 3
"""
# speech/actions.py — hardened helpers
import logging
import ntpath

from utils.redact import redact_transcript
import subprocess
import asyncio
from datetime import datetime
import webbrowser
from typing import Optional, Any, Dict, Union
from urllib.parse import quote_plus
from ai.providers.openai_compat import ChatStatus

logger = logging.getLogger(__name__)

# Reserved activate-target keyword: resolved to the default browser's
# executable name at command time (see _default_browser_exe).
DEFAULT_BROWSER_TARGET = "default_browser"

# Shell host executables that a registry shell-open command may name instead
# of the browser itself (e.g. ``rundll32.exe url.dll,FileProtocolHandler %1``).
# Activating one of these would silently do nothing, so the parser treats
# them as "no executable found" and the msedge.exe fallback fires instead
# (wh-user-patterns-split.12.2).
_SHELL_HOST_EXES = {
    "rundll32.exe",
    "launchwinapp.exe",
    "openwith.exe",
    "explorer.exe",
}


def _exe_name_from_command(command: Any) -> Optional[str]:
    """Extract the executable file name from a registry shell-open command.

    Registry commands look like ``"C:\\...\\brave.exe" -- "%1"`` (quoted) or
    ``C:\\PROGRA~1\\...\\firefox.exe -osint -url "%1"`` (unquoted). Returns
    the basename (e.g. ``brave.exe``), or None when no ``.exe`` path can be
    parsed out or the path names a shell host (rundll32-style handler)
    rather than the browser itself.
    """
    if not isinstance(command, str):
        return None
    command = command.strip()
    if not command:
        return None
    if command.startswith('"'):
        closing = command.find('"', 1)
        if closing == -1:
            return None
        path = command[1:closing]
    else:
        path = command.split()[0]
    exe = ntpath.basename(path)
    if not exe.lower().endswith(".exe"):
        return None
    if exe.lower() in _SHELL_HOST_EXES:
        return None
    return exe


def _default_browser_exe() -> str:
    """Resolve the default browser's executable name from the Windows registry.

    Reads the user's HTTP handler choice (UserChoice ProgId), then that
    handler's shell-open command, and returns the executable's basename.
    Falls back to ``msedge.exe`` — present on every supported Windows
    machine — when the lookup or the parse fails, so the "browser" voice
    command still does something sensible.
    """
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations"
            r"\UrlAssociations\http\UserChoice",
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, str(prog_id) + r"\shell\open\command"
        ) as key:
            command, _ = winreg.QueryValueEx(key, "")
    except (OSError, ImportError) as exc:
        logger.warning(
            "Default-browser registry lookup failed (%s); using msedge.exe", exc
        )
        return "msedge.exe"
    exe = _exe_name_from_command(command)
    if not exe:
        logger.warning(
            "Could not parse default-browser command %r; using msedge.exe", command
        )
        return "msedge.exe"
    return exe

_WORD_TO_INT_MAP = {
    "zero": 0, "one": 1, "two": 2, "to": 2, "too": 2, "three": 3, "four": 4, "for": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10
}

def words_to_int(text: Optional[str]) -> Optional[int]:
    """Convert word or digit string to integer for numeric parameter parsing.

    Supports both digit strings ("3") and word strings ("three", "five").
    Returns 1 as default if text is None (for commands like "go up" without a count).
    Returns None if text cannot be converted.

    Args:
        text: String to convert (e.g., "3", "three", "five"), or None for default

    Returns:
        Integer value if conversion succeeds, 1 if text is None, None if invalid
    """
    if text is None: return 1
    text = str(text).lower().strip()
    if text.isdigit(): return int(text)
    if text in _WORD_TO_INT_MAP: return _WORD_TO_INT_MAP[text]
    return None

# Spoken key name aliases - maps speech variations to VK_CODE_MAP keys
# Single strings map to one key; tuples map to key combinations (e.g., shifted chars)
SPOKEN_KEY_MAP = {
    # Modifiers (spoken variations)
    "control": "ctrl",
    "windows": "win",

    # Multi-word keys
    "page up": "pageup",
    "page down": "pagedown",
    "print screen": "printscreen",
    "caps lock": "capslock",

    # Spoken variations
    "escape": "esc",
    "return": "enter",

    # Punctuation/symbols (speakable names)
    "backtick": "`",
    "tilde": "~",
    "semicolon": ";",
    "colon": ":",
    "slash": "/",
    "forward slash": "/",
    "backslash": "\\",
    "back slash": "\\",
    "pipe": "|",
    "question": "?",
    "question mark": "?",
    "comma": ",",
    "period": ".",
    "dot": ".",
    "quote": '"',
    "double quote": '"',
    "single quote": "'",
    "apostrophe": "'",
    "left bracket": "[",
    "right bracket": "]",
    "left brace": "{",
    "right brace": "}",
    "open bracket": "[",
    "close bracket": "]",
    "open brace": "{",
    "close brace": "}",
    "left parenthesis": ("shift", "9"),
    "right parenthesis": ("shift", "0"),
    "left paren": ("shift", "9"),
    "right paren": ("shift", "0"),
    "open parenthesis": ("shift", "9"),
    "close parenthesis": ("shift", "0"),
    "open paren": ("shift", "9"),
    "close paren": ("shift", "0"),
    "less than": "<",
    "greater than": ">",
    "equals": "=",
    "equal": "=",
    "plus": "+",
    "minus": "-",
    "hyphen": "-",
    "dash": "-",
    "underscore": "_",
    "hash": "#",
    "hashtag": "#",
    "pound": "#",
    "at": "@",
    "at sign": "@",
    "ampersand": "&",
    "and sign": "&",
    "asterisk": "*",
    "star": "*",
    "caret": "^",
    "carrot": "^",
    "percent": "%",
    "dollar": "$",
    "dollar sign": "$",
    "exclamation": "!",
    "bang": "!",
}

# Modifier keys that should be pressed first in key combinations
_MODIFIER_KEYS = {'ctrl', 'alt', 'shift', 'win', 'lwin'}


def _normalize_key(word: str) -> Optional[Union[str, tuple]]:
    """Normalize a spoken key word to VK_CODE_MAP key name.

    Returns:
        - str: single key name
        - tuple: multiple keys (e.g., ("shift", "9") for parenthesis)
        - None: if key is not recognized
    """
    from services.wheelhouse.utils.win_input_sender import VK_CODE_MAP

    word_lower = word.lower().strip()

    # Check spoken alias map first (may return str or tuple)
    if word_lower in SPOKEN_KEY_MAP:
        return SPOKEN_KEY_MAP[word_lower]

    # Check if already a valid VK_CODE_MAP key
    if word_lower in VK_CODE_MAP:
        return word_lower

    return None


class ActionFunctions:
    """
    Functions callable from the command parser.
    UI-bound functions return dict payloads; local functions run here.
    """
    def __init__(self, speech_handler):
        self._functions: Dict[str, Any] = {}
        self.speech_handler = speech_handler
        self._register_functions()

    def _register_functions(self) -> None:
        # UI command builders
        self._functions["hk"] = self.hotkey
        self._functions["press"] = self.press
        self._functions["press_keys"] = self.press_keys
        self._functions["activate"] = self.activate_window
        self._functions["literal"] = self.handle_literal
        self._functions["type_text"] = self.type_text
        self._functions["insert_text"] = self.insert_text
        self._functions["insert_raw"] = self.insert_raw
        self._functions["insert_newlines"] = self.insert_newlines
        self._functions["transform_selection"] = self.transform_selection
        self._functions["text"] = self.text  # Wrapper for replacement patterns
        self._functions["number_point"] = self.number_point
        self._functions["wrap_or_insert"] = self.wrap_or_insert
        self._functions["skip_clipboard_restore"] = self.skip_clipboard_restore
        # Local
        self._functions["run"] = self.run_program
        self._functions["sleep"] = self.async_sleep
        self._functions["date"] = self.format_date
        self._functions["gs"] = self.GSearch
        self._functions["capture_clipboard"] = self.capture_clipboard
        self._functions["add_hint_to_stt"] = self.add_hint_to_stt
        self._functions["cursor_navigate"] = self.cursor_navigate
        self._functions["click_element"] = self.click_element
        self._functions["show_overlay_command"] = self.show_overlay_command
        self._functions["hide_overlay_command"] = self.hide_overlay_command
        # AI Service
        self._functions["fix_text_ai"] = self.fix_text_ai
        self._functions["cancel_fix"] = self.cancel_fix
        self._functions["wheelhouse_help"] = self.wheelhouse_help
        self._functions["wheelhouse_help_online"] = self.wheelhouse_help_online
        # Pattern Manager
        self._functions["open_pattern_manager"] = self.open_pattern_manager
        # Mode switching
        self._functions["set_speech_interaction_mode"] = self.set_speech_interaction_mode

    def get_functions(self):
        """Returns the function registry for action lookup.
        
        Returns:
            Dict[str, callable]: Mapping of function names to action functions
        """
        return self._functions

    # ---- UI command payloads ----
    def handle_literal(self, text):
        """
        :flow: Command and Dictation Routing
        :step: 4a
        :description: Bypasses all pattern processing for literal text insertion.
        :data_in: Raw text from "literal <text>" command.
        :data_out: UI action payload for direct text typing.
        :consumes_from: Speech Processing
        :produces_for: UI Action Execution
        :notes: Special bypass handler for the "literal" command pattern, which allows
        inserting text without any pattern matching or transformation. This is
        the escape hatch when users need to dictate text that would otherwise
        match command patterns.
        """
        return {"action": "type_text", "params": {"text": text}}
    def type_text(self, text: str):
        """
        :flow: Command and Dictation Routing
        :step: 4b
        :produces_for: UI Action Execution
        :description: Creates UI command payload for raw text typing without intelligent spacing.
        :data_in: text (string to type).
        :data_out: Dict payload `{"action": "type_text", "params": {"text": text}}`.
        :notes: Unlike insert_text/intelligent_insert_text, this bypasses cursor detection and spacing logic.
            Used for patterns that need exact character-by-character typing without modification.
        """
        return {"action": "type_text", "params": {"text": text}}
    
    def text(self, template: str):
        """
        Wrapper for replacement patterns - inserts text with intelligent spacing.

        This is the standard function for text replacement patterns in the unified
        pattern system. It calls insert_text() to use intelligent_insert_text IPC,
        which handles spacing and cursor positioning.

        When template is empty, returns None to silently consume the matched text
        without sending anything to the input process (used by non-speech sound
        patterns like *cough* and assistant-filter patterns like "okay Google").

        Args:
            template: Text to insert (may contain resolved g1/g2/g3 backreferences)

        Returns:
            Dictionary payload for intelligent text insertion, or None if empty
        """
        if not template:
            return None
        return self.insert_text(template)

    def number_point(self, number_word: str):
        """Convert number word to digit with period for numbered lists.

        Used by "point [number]" command: "point one" -> "1."

        Args:
            number_word: Number as word ("one") or digit ("1")

        Returns:
            Dictionary payload for intelligent text insertion
        """
        num = words_to_int(number_word)
        if num is None:
            return self.insert_text(f"{number_word}.")
        return self.insert_text(f"{num}.")

    def insert_text(self, text: str):
        """Creates the payload for intelligent text insertion.

        :flow: Command and Dictation Routing
        :step: 4c
        :produces_for: UI Action Execution
        :description: Packages the finalized dictation string into the UI command payload that the TextParser hands off to the IPC layer.
        :data_in: Finalized insertion string produced by the command pipeline.
        :data_out: Dict payload `{"action": "intelligent_insert_text", "params": {...}}` forwarded to UI Action Execution.
        """
        return {"action": "intelligent_insert_text", "params": {"insertion_string": text}}
    
    def insert_raw(self, text: str):
        """Insert literal text at cursor without intelligent insertion.

        No prefix space, no capitalization, no TextPerfector processing.
        Used for inserting exact character sequences like symbols, codes, etc.

        Returns:
            Dictionary payload for raw text insertion via clipboard paste.
        """
        return {"action": "raw_insert_text", "params": {"text": text}}

    def insert_newlines(self, count_str):
        """Insert multiple newline characters into text.
        
        Args:
            count_str: Number of newlines to insert (as string from regex capture)
        
        Returns:
            Dictionary payload for intelligent text insertion with newlines
        """
        count = words_to_int(count_str)
        if count is None or count < 1:
            count = 1
        if count > 50:
            count = 50
        newlines = "\n" * count
        return {"action": "intelligent_insert_text", "params": {"insertion_string": newlines}}
    
    def transform_selection(self, transformation_type: str):
        """Transform selected text with wrapping or case conversion.

        Args:
            transformation_type: Type of transformation (quote, bracket, snake_case, etc.)

        Returns:
            Dictionary payload for selection transformation

        :flow: Selection Text Transformation
        :step: 1
        :produces_for: Selection Text Transformation
        :description: Queues a selection transformation request for the UI process via IPC.
        :notes: Triggered by TextParser for commands like "snake case" or "quote". The UI
            process performs clipboard-based wrapping or case conversion (14 supported types).
        :data_in: Transformation type string from command pattern match
        :data_out: Dictionary payload {'action': 'transform_selection', 'params': {...}}
            sent via IPC to UI process
        """
        return {"action": "transform_selection", "params": {"transformation_type": transformation_type}}
    
    def wrap_or_insert(self, left_fence: str, right_fence: str, text: str = ""):
        """Handle wrapping operations: intelligently wrap selection, wrap text, or insert empty delimiters.
        
        This delegates to the UI layer which will:
        1. Check if text is selected (sentinel check) → wrap selection if exists
        2. If no selection but captured text → insert wrapped text  
        3. If no selection and no text → insert empty delimiters with cursor between
        
        Used with (.*)$ pattern to capture everything after the trigger word:
        - "quote hello" captures " hello" → inserts '"hello"'
        - "quote" captures "" → uses intelligent logic in UI layer
        
        Args:
            left_fence: Opening delimiter (e.g., "(", "[", "<", "{", "'", '"')
            right_fence: Closing delimiter (e.g., ")", "]", ">", "}", "'", '"')
            text: Captured text from pattern (may be empty string)
        
        Returns:
            Dictionary payload for wrap_or_insert UI action
            
        Examples:
            wrap_or_insert("(", ")", " hello") → UI inserts "(hello)"
            wrap_or_insert("(", ")", "") → UI checks selection/inserts empty
            wrap_or_insert('"', '"', " world") → UI inserts '"world"'
            wrap_or_insert('"', '"', "") → UI checks selection/inserts empty
        """
        return {
            "action": "wrap_or_insert",
            "params": {
                "left_fence": left_fence,
                "right_fence": right_fence,
                "text": text
            }
        }
    
    def hotkey(self, *keys: Any):
        """Execute a hotkey combination, optionally repeated.
        
        :flow: Command and Dictation Routing
        :step: 5
        :produces_for: UI Action Execution
        :description: Packages hotkey commands with optional repeat counts and None filtering.
            Handles patterns like "undo 3" where optional regex groups may capture None.
        :data_in: Variable args from pattern match (keys + optional numeric group)
        :data_out: Dictionary payload {'action': 'hotkey_action', 'params': {'keys': [...], 'repeat': N}}
        
        None Filtering Logic:
        - Checks if last argument is None or string "None" (from unmatched optional group)
        - Filters out None before numeric conversion
        - Prevents validation errors when optional group like (\\d+)? doesn't match
        
        Repeat Count Logic:
        - Last argument converted to int via words_to_int()
        - If valid number, treated as repeat count (removed from keys list)
        - If not a number, treated as regular key
        - Capped at 50 repetitions for safety
        
        Examples:
        - hotkey("ctrl", "z", "3") → {keys: ["ctrl", "z"], repeat: 3}
        - hotkey("ctrl", "z", None) → {keys: ["ctrl", "z"], repeat: 1}
        - hotkey("ctrl", "z", "nonsense") → {keys: ["ctrl", "z", "nonsense"], repeat: 1}

        Args:
            *keys: Keys to press together (e.g., "ctrl", "c")
                   Last argument can be a repeat count (str or int) or None from optional group

        Returns:
            Dictionary payload for hotkey action with repeat parameter
        """
        # Check if last argument is a repeat count
        if len(keys) > 0:
            last_key = keys[-1]
            # Filter out None values from unmatched regex groups
            if last_key is None or str(last_key) == "None":
                keys = keys[:-1]
                repeat_count = 1
            else:
                repeat_count = words_to_int(str(last_key))
                
                # If last arg is a valid number, treat it as repeat count
                if repeat_count is not None and repeat_count > 0:
                    keys = keys[:-1]  # Remove repeat count from keys
                    if repeat_count > 50:
                        repeat_count = 50
                else:
                    repeat_count = 1
        else:
            repeat_count = 1
        
        logger.debug(f"Hotkey: {keys}, repeat: {repeat_count}")
        return {"action": "hotkey_action", "params": {"keys": [str(k) for k in keys], "repeat": repeat_count}}
    
    def press(self, key, repeat_str=None):
        """
        :flow: Command and Dictation Routing
        :step: 4d
        :produces_for: UI Action Execution
        :description: Creates UI command payload for keyboard key press with optional repeat count.
        :data_in: key (key name string), repeat_str (optional capture group with count as text).
        :data_out: Dict payload `{"action": "press_key_action", "params": {"key": str, "repeat": int}}`.
        :notes: Converts repeat_str to integer via words_to_int (handles both digits and words like "five").
            Clamps repeat count between 1-50 to prevent accidental excessive input. Used by patterns like
            "delete 3" or "backspace five".
        """
        repeat_count = words_to_int(repeat_str)
        if repeat_count is None or repeat_count < 1: repeat_count = 1
        if repeat_count > 50: repeat_count = 50
        logger.debug("Pressing key %r, repeat %d", key, repeat_count)
        return {"action": "press_key_action", "params": {"key": str(key), "repeat": repeat_count}}

    def press_keys(self, key_sequence: str):
        """Execute a spoken key combination.

        Parses spoken key names and executes as hotkey.
        Key order doesn't matter - modifiers are always pressed first.
        If any key is unrecognized, returns None (phrase treated as dictation).

        Tuple entries in SPOKEN_KEY_MAP (e.g., ("shift", "9") for parenthesis)
        are expanded into the key list.

        Args:
            key_sequence: Space-separated key names (e.g., "control alt delete")

        Returns:
            Dictionary payload for hotkey action, or None if unrecognized keys
        """
        if not key_sequence:
            logger.warning("press_keys: Empty key sequence")
            return None

        words = key_sequence.lower().strip().split()

        # Expand hyphenated tokens from Whisper (e.g., "f-11", "control-alt")
        # Pass 1: try dehyphenated ("f-11" -> "f11"), if that's a known key, use it
        # Pass 2: split on hyphens ("control-alt" -> ["control", "alt"])
        expanded = []
        for w in words:
            if '-' in w and w not in SPOKEN_KEY_MAP:
                dehyphenated = w.replace('-', '')
                if _normalize_key(dehyphenated) is not None:
                    expanded.append(dehyphenated)
                else:
                    expanded.extend(w.split('-'))
            else:
                expanded.append(w)
        words = expanded

        normalized_keys = []
        i = 0

        while i < len(words):
            # Try two-word combination first (e.g., "page up", "left brace")
            if i + 1 < len(words):
                two_word = f"{words[i]} {words[i+1]}"
                if two_word in SPOKEN_KEY_MAP:
                    value = SPOKEN_KEY_MAP[two_word]
                    # Handle tuple entries (e.g., ("shift", "9") for parenthesis)
                    if isinstance(value, tuple):
                        normalized_keys.extend(value)
                    else:
                        normalized_keys.append(value)
                    i += 2
                    continue

            # Try single word
            normalized = _normalize_key(words[i])
            if normalized:
                # Handle tuple entries from SPOKEN_KEY_MAP
                if isinstance(normalized, tuple):
                    normalized_keys.extend(normalized)
                else:
                    normalized_keys.append(normalized)
            else:
                # Unrecognized key - fail entire command
                logger.info(f"press_keys: Unrecognized key '{redact_transcript(words[i])}', treating as dictation")
                return None
            i += 1

        if not normalized_keys:
            logger.warning("press_keys: No keys parsed")
            return None

        # Sort: modifiers first, then other keys (order-independent)
        modifiers = [k for k in normalized_keys if k in _MODIFIER_KEYS]
        non_modifiers = [k for k in normalized_keys if k not in _MODIFIER_KEYS]
        sorted_keys = modifiers + non_modifiers

        logger.debug(f"press_keys: '{key_sequence}' -> {sorted_keys}")
        return self.hotkey(*sorted_keys)

    def activate_window(self, target):
        """Activate a window by process name (e.g., 'brave.exe') or title pattern.
        If target ends with .exe, search by process name. Otherwise, search by title.
        The reserved target 'default_browser' is resolved to the default
        browser's executable name at command time.

        :flow: Window Activation
        :step: 1
        :produces_for: Window Activation
        :description: Creates the activation command payload.
        :data_in: Target window name/executable from voice command.
        :data_out: Payload {'action': 'activate_window', 'params': {'target': target}}.
        """
        target = str(target)
        if target == DEFAULT_BROWSER_TARGET:
            target = _default_browser_exe()
        return {"action": "activate_window", "params": {"target": target}}

    # ---- Local actions ----
    async def run_program(self, program_path):
        try: await asyncio.to_thread(subprocess.Popen, str(program_path), shell=True)
        except Exception as e: logger.error("run_program error: %s", e)
        return None
    def format_date(self, format_string="%Y-%m-%d"):
        """Formats current date/time using strftime format string.
        
        Args:
            format_string: Python datetime strftime format (default: YYYY-MM-DD)
            
        Returns:
            Formatted date string
        """
        return datetime.now().strftime(format_string)
    async def GSearch(self, query=None):
        """Open Google search with specified query.

        Args:
            query: Search query string. If None or empty, opens blank Google search.
        """
        if query is None or query == "":
            query = ""
        url = "https://www.google.com/search?q=" + quote_plus(str(query))
        try: await asyncio.to_thread(webbrowser.open, url)
        except Exception as e: logger.error("GSearch error: %s", e)
        return None
    async def async_sleep(self, duration_str: str):
        try:
            d = float(duration_str); await asyncio.sleep(max(0.0, d))
        except Exception:
            logger.warning("Invalid sleep duration: %r", duration_str)
        return None

    def capture_clipboard(self):
        """Capture current clipboard content for use in subsequent actions.

        Returns the clipboard content as a string so it can be stored in the
        command execution context and passed to subsequent actions.

        Usage in patterns.toml:
            { function = "capture_clipboard", params = [] }

        The captured value can then be used by referencing "capture_clipboard"
        in subsequent action params.

        Returns:
            str: Current clipboard content
        """
        try:
            import pyperclip
            content = pyperclip.paste()
            logger.debug(f"Captured clipboard: '{redact_transcript(content)}'")
            return content
        except Exception as e:
            logger.error(f"Failed to capture clipboard: {e}")
            return ""

    def skip_clipboard_restore(self, enable=True):
        """Control whether clipboard restoration happens at utterance end.

        When enabled, prevents the utterance clipboard manager from restoring
        the original clipboard content. This is needed for copy/cut commands
        where the user explicitly wants to modify the clipboard.

        The flag is automatically cleared after command completion via the
        command_engine's finally block.

        Args:
            enable: If True, skip clipboard restoration. Defaults to True.

        Usage in patterns.toml:
            { function = "skip_clipboard_restore", awaits_done = true }

        Returns:
            dict: UI action payload to set the skip flag
        """
        return {
            "action": "skip_clipboard_restore",
            "params": {"enable": bool(enable)}
        }

    async def add_hint_to_stt(self):
        """Add selected text from clipboard to STT hints list via WebSocket command.
        
        This function is triggered by the "x-ray boost" voice command. It:
        1. Captures the current clipboard content (which should contain selected text)
        2. Validates the hint text
        3. Sends a WebSocket command to the STT server to add the hint
        
        The STT server will update its config.toml file and reload the hints.
        
        Usage in patterns.toml:
            [[pattern]]
            pattern = '''^boost$'''
            requires_hotword = true
            actions = [
                { function = "skip_clipboard_restore", awaits_done = true },
                { function = "hk", params = ["ctrl", "c"], awaits_done = true },
                { function = "add_hint_to_stt", awaits_done = true }
            ]
        
        Returns:
            None (async function that sends WebSocket command)
        """
        try:
            import pyperclip
            
            # Get the selected text from clipboard
            hint_text = pyperclip.paste().strip()
            
            if not hint_text:
                logger.warning("add_hint_to_stt: No text in clipboard to add as hint")
                return None
            
            # Validate hint length (reasonable limits)
            if len(hint_text) > 100:
                logger.warning(f"add_hint_to_stt: Hint text too long ({len(hint_text)} chars), truncating to 100")
                hint_text = hint_text[:100]
            
            # Get websocket_manager from app
            if not hasattr(self.speech_handler, 'app') or not self.speech_handler.app:
                logger.error("add_hint_to_stt: No app reference available")
                return None
            
            websocket_manager = getattr(self.speech_handler.app, 'websocket_manager', None)
            if not websocket_manager:
                logger.error("add_hint_to_stt: No websocket_manager available")
                return None
            
            # Send WebSocket command to STT server
            logger.info(f"add_hint_to_stt: Sending hint to STT server: '{redact_transcript(hint_text)}'")
            await websocket_manager.send_command_to_stt("add_hint", hint=hint_text)
            
        except ImportError:
            logger.error("add_hint_to_stt: pyperclip not available")
        except Exception as e:
            logger.error(f"add_hint_to_stt error: {e}", exc_info=True)

        return None

    async def cursor_navigate(self, utterance: str):
        """Parse and execute cursor navigation commands.

        Parses the utterance into NavigationCommands. For valid commands,
        sends hotkey actions sequentially via IPC. For invalid utterances,
        falls through to dictation (returns insert_text action dict).
        """
        from .navigation.parser import NavigationParser
        from .navigation.executor import NavigationExecutor

        commands = NavigationParser.parse(utterance)
        if not commands:
            # Send directly - command_engine discards return values from async functions
            await self.speech_handler.app.send_command(self.insert_text(utterance))
            return None

        actions = NavigationExecutor.to_actions(commands)
        for action in actions:
            await self.speech_handler.app.send_request(action["action"], action["params"])
        return None

    async def click_element(self, target_text: str):
        """Execute a 'click <target>' command end-to-end (wh-tab7j).

        wh-vjwdl built ``ClickCommandParser.parse``; this slice (wh-tab7j)
        wires the full command-to-response flow:

          1. Parse the spoken target (group g1) into an ``ElementQuery``.
             On unparseable input (empty / whitespace-only / collapses to
             an empty name) return None so the phrase falls through to
             dictation -- nothing crosses the process boundary.
          2. Generate a trace_id and push it onto the trace contextvar so
             every Logic / Input log line and the IPC envelope share one
             correlation id (matching the rejection-toast and soft-allow
             flows).
          3. Delegate to ``LogicController.forward_click_element``, which
             owns the config gate, the Input-process round trip with the
             ``[click] response_timeout_ms`` timeout, and the timeout /
             malformed-response degrade paths.

        Returns None on every path. The command engine treats a None
        return from an async local function as "nothing to send" (the
        send_request happens INSIDE forward_click_element, with the click
        timeout, NOT via the generic awaits_done path which would wrongly
        use WheelHouseApp.response_timeout_s).
        """
        import uuid

        from .click_parser import ClickCommandParser
        from utils.trace_context import set_trace, get_trace_id

        query = ClickCommandParser.parse(target_text)
        if query is None:
            logger.info("click_element: unparseable target %r; ignoring", redact_transcript(target_text))
            return None

        # Generate the trace_id at parse time and push the contextvar. Reuse
        # an already-set trace_id when the pipeline established one for this
        # utterance; otherwise mint a fresh click-scoped id.
        trace_id = get_trace_id() or f"click-{uuid.uuid4().hex[:12]}"
        set_trace(trace_id)

        lc = getattr(self.speech_handler, "logic_controller", None)
        if lc is None or not hasattr(lc, "forward_click_element"):
            logger.error(
                "click_element: no logic_controller.forward_click_element "
                "available; cannot execute (trace_id=%s)", trace_id,
            )
            return None

        logger.info(
            "click_element: parsed query name=%r role=%r trace_id=%s",
            query.name, query.role, trace_id,
        )
        await lc.forward_click_element(query, trace_id)
        return None

    async def show_overlay_command(self):
        """Handle the 'show numbers' voice command (wh-n29v.17).

        Mints (or reuses) a click-scoped trace_id and delegates to
        ``LogicController.handle_overlay_command('show', trace_id)``, which
        applies SHOW_NUMBERS to the overlay state machine and hands the
        returned effects to the integration stub seam. Mirrors the thin
        action -> LogicController delegation ``click_element`` uses. Returns
        None on every path (nothing crosses the dictation path).
        """
        return await self._delegate_overlay_command("show")

    async def hide_overlay_command(self):
        """Handle the 'hide numbers' voice command (wh-n29v.17).

        Delegates to ``LogicController.handle_overlay_command('hide',
        trace_id)``, which applies HIDE_NUMBERS to the overlay state machine
        (immediate close per r2.4) and hands the returned effects to the
        integration stub seam. Returns None on every path.
        """
        return await self._delegate_overlay_command("hide")

    async def _delegate_overlay_command(self, command: str):
        """Shared show/hide delegation to LogicController (wh-n29v.17)."""
        import uuid

        from utils.trace_context import set_trace, get_trace_id

        trace_id = get_trace_id() or f"click-{uuid.uuid4().hex[:12]}"
        set_trace(trace_id)

        lc = getattr(self.speech_handler, "logic_controller", None)
        if lc is None or not hasattr(lc, "handle_overlay_command"):
            logger.error(
                "%s numbers: no logic_controller.handle_overlay_command "
                "available; cannot execute (trace_id=%s)", command, trace_id,
            )
            return None

        logger.info("%s numbers: trace_id=%s", command, trace_id)
        await lc.handle_overlay_command(command, trace_id)
        return None

    async def open_pattern_manager(self):
        """Open the Pattern Manager dialog via GUI IPC."""
        self._send_gui_action({"action": "open_pattern_manager"})
        logger.info("Sent open_pattern_manager to GUI")
        return None

    def set_speech_interaction_mode(self, mode: str):
        """Switch speech interaction mode between 'toggle' and 'push_to_talk'."""
        lc = getattr(self.speech_handler, 'logic_controller', None)
        if lc:
            sm = getattr(lc, 'state_manager', None)
            if sm:
                sm.set_speech_interaction_mode(mode)
                display_name = "Push to talk mode" if mode == "push_to_talk" else "Click to talk mode"
                self._send_gui_action({
                    "action": "show_notification",
                    "title": "WheelHouse",
                    "message": display_name,
                    "timeout": 3,
                })
                logger.info(f"Speech interaction mode set to: {mode}")
            else:
                logger.warning("Cannot set interaction mode -- state_manager not available")
        else:
            logger.warning("Cannot set interaction mode -- logic_controller not available")
        return None

    def _send_gui_action(self, action_dict: dict) -> None:
        """Send an action to the GUI process via state_to_gui_queue."""
        lc = getattr(self.speech_handler, 'logic_controller', None)
        if lc:
            sm = getattr(lc, 'state_manager', None)
            if sm:
                try:
                    sm.state_to_gui_queue.put_nowait(action_dict)
                except Exception:
                    pass

    # ---- AI Service actions ----

    def _get_ai_service(self):
        """Get AIService from ServiceManager, if available.

        Existence-only: returns the service whenever the speech_handler ->
        logic_controller -> service_manager -> ai_service chain resolves. The
        old ``if svc and not svc._provider: return None`` readiness gate was
        removed (finding 1.9): readiness is now a transient, re-probed property
        and is checked at the action level (fix_text_ai / cancel_fix) via
        ai.is_ready(), not by reaching into the provider here.
        """
        lc = getattr(self.speech_handler, 'logic_controller', None)
        if not lc:
            return None
        sm = getattr(lc, 'service_manager', None)
        if not sm:
            return None
        return getattr(sm, 'ai_service', None)

    async def fix_text_ai(self):
        """Capture text from focused element, correct via AI, paste back."""
        ai = self._get_ai_service()
        if not ai:
            logger.warning("fix_text_ai: AIService not available")
            return None

        # Readiness gate moved here (finding 1.9). When AI is off / unreachable
        # speak a graceful notice instead of failing silently (design s7).
        if not ai.is_ready():
            await ai.speak("AI is not available right now.")
            return None

        if ai.is_processing():
            await ai.speak("Already processing, please wait.")
            return None

        async with ai._processing_lock:
            # Step 1: Capture text via Input Process
            result = await self.speech_handler.app.send_request(
                "capture_selected_text", params={}
            )
            text = result.get("text", "")
            logger.debug("fix_text_ai: captured %d chars", len(text))
            if not text or not text.strip():
                await ai.speak("No text to correct.")
                return None

            # Step 2: Word-count notice for large text (no time estimate -- the
            # thin client has no local-tier basis for a seconds estimate, so the
            # estimate_correction_time call and 'roughly N seconds' wording were
            # dropped; design s4).
            word_count = len(text.split())
            if word_count > 200:
                await ai.speak(f"About {word_count} words.")

            await ai.speak_brief("Correcting.")
            self._send_gui_action({"action": "show_working", "message": "Correcting..."})

            try:
                # Step 3: Send to AI for correction (returns a ChatResult).
                corrected = await ai.fix_text(text)

                # Step 3a: Cancellation is a distinct outcome -- do NOT probe
                # the server or speak an error (finding wh-ay6h.6.4).
                if corrected.status is ChatStatus.CANCELLED:
                    await ai.speak_brief("Cancelled.")
                    return None

                if not corrected.ok:
                    logger.warning(
                        "fix_text_ai: correction not ok (status=%s)",
                        corrected.outcome,
                    )
                    # MODEL_NOT_FOUND means the server responded (404 on
                    # the model), so a reachability re-probe would only
                    # mislead -- name the real problem instead (wh-75m).
                    if corrected.status is ChatStatus.MODEL_NOT_FOUND:
                        await ai.speak(
                            "The AI server doesn't have the configured "
                            "model. Check the model name in the AI "
                            "settings. Original text preserved."
                        )
                        return None
                    # A reasoning model that spent the whole budget on
                    # hidden thinking also responded fine at the HTTP
                    # level -- name the real problem
                    # (wh-ai-reasoning-model-empty).
                    if corrected.exhausted_reasoning:
                        await ai.speak(
                            "The AI model spent its whole answer budget "
                            "on hidden reasoning and returned nothing. "
                            "Configure a non-reasoning model. "
                            "Original text preserved."
                        )
                        return None
                    # Re-probe reachability before the 'isn't responding'
                    # wording so a server that just recovered is not maligned
                    # (s7 / decision 27).
                    if not await ai.recheck_ready():
                        await ai.speak(
                            "The AI server isn't responding. "
                            "Original text preserved."
                        )
                    else:
                        await ai.speak("Correction failed. Original text preserved.")
                    return None

                corrected_text = corrected.text

                # Step 4: Check cancellation before pasting (race between the
                # AI response arriving and a concurrent cancel_fix call).
                if ai.cancel_requested:
                    ai.cancel_requested = False
                    await ai.speak_brief("Cancelled.")
                    return None

                # Step 5: Replace with corrected text via Input Process
                if corrected_text != text:
                    await self.speech_handler.app.send_request(
                        "replace_selected_text", params={"text": corrected_text}
                    )
                    await ai.speak_brief("Done.")
                else:
                    await ai.speak_brief("No changes needed.")
            finally:
                self._send_gui_action({"action": "hide_working"})

        return None

    async def cancel_fix(self):
        """Set cancellation flag. Checked between AI response and paste."""
        ai = self._get_ai_service()
        if ai and ai.is_processing():
            ai.cancel_requested = True
            await ai.speak_brief("Cancelling.")
        return None

    async def wheelhouse_help(self, question: str = ""):
        """Open the local help chat window, optionally with a spoken question.

        If question is provided (from 'wheelhouse help [question]' pattern),
        the chat window opens AND the question is submitted immediately.
        """
        payload = {"action": "show_help_chat"}
        if question:
            payload["question"] = question
        self._send_gui_action(payload)
        return None

    async def wheelhouse_help_online(self):
        """Open cloud help in browser (Gemini Gem)."""
        lc = getattr(self.speech_handler, "logic_controller", None)
        if not lc:
            return None

        config = getattr(lc, "config_service", None)
        if not config:
            return None

        gem_url = config.get("ai.help.gem_url", "")
        if not gem_url:
            ai = self._get_ai_service()
            if ai:
                await ai.speak_brief("Online help is not configured.")
            return None

        import webbrowser
        await asyncio.to_thread(webbrowser.open, gem_url)
        return None