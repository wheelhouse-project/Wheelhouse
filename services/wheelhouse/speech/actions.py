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
import codecs
import contextlib
import logging
import math
import ntpath

from utils.redact import redact_transcript
import subprocess
import asyncio
from datetime import datetime
import webbrowser
from typing import Optional, Any, Dict, Union
from urllib.parse import quote_plus
from ai.providers.openai_compat import ChatStatus
from .actions_config import (
    RUN_CAPTURE_TIMEOUT_CEILING_S,
    RUN_CAPTURE_TIMEOUT_FLOOR_S,
    ActionsConfig,
)
from .number_word_parser import parse_number_word

logger = logging.getLogger(__name__)

_RUN_CAPTURE_READ_CHUNK_BYTES = 4 * 1024
_RUN_CAPTURE_STDERR_LOG_BYTES = 4 * 1024


class ActionFailed(Exception):
    """An action could not run, so the whole matched rule must be abandoned.

    wh-arrow-key-names-missing: an action that returned ``None`` to report
    failure could not be told apart from the many actions that return
    ``None`` on success (``run_program``, ``async_sleep``, the grid
    commands). ``TextParser._execute_rule`` therefore returned ``True`` for
    a failed ``press_keys`` call, ``parse_and_execute`` reported a match,
    and ``SpeechProcessor._execute_command`` consumed the spoken words
    without pressing anything or typing anything.

    Raising instead makes the failure explicit. ``_execute_rule`` catches
    this exception, abandons the remaining steps, and returns ``False``, so
    the speech processor sends the utterance to dictation and the words
    reach the screen.
    """


class _RunCaptureOutputCapExceeded(Exception):
    """Raised once stdout cannot fit in the configured stored-result cap."""


async def _terminate_run_capture_process(process, drain_streams=()) -> None:
    """Stop a child promptly, then discard supplied pipes while it exits."""
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    # Signal the child before scheduling any discard readers.  Starting the
    # readers first could unblock a pipe-flooding child long enough for it to
    # finish successfully instead of observing the cap failure.
    drain_tasks = [
        asyncio.create_task(_drain_run_capture_stream(stream))
        for stream in drain_streams
        if stream is not None
    ]
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()
    finally:
        await asyncio.gather(*drain_tasks, return_exceptions=True)


async def _read_run_capture_stdout(stream, output_cap_chars: int) -> tuple[str, bool]:
    """Decode stdout incrementally, retaining no more than the stored cap."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    confirmed_parts: list[str] = []
    pending_trailing: list[str] = []
    confirmed_characters = 0
    pending_characters = 0
    discarded_trailing = False

    def consume(decoded: str) -> bool:
        """Append decoded text, returning True when stored output is over the cap."""
        nonlocal confirmed_characters, pending_characters, discarded_trailing
        for character in decoded:
            if character in "\r\n":
                if not discarded_trailing:
                    if confirmed_characters + pending_characters < output_cap_chars:
                        pending_trailing.append(character)
                        pending_characters += 1
                    else:
                        # These bytes are stripped at EOF.  Once retaining
                        # them would exceed the cap, they cannot be stored;
                        # any later content character must fail instead.
                        pending_trailing.clear()
                        pending_characters = 0
                        discarded_trailing = True
                continue

            if discarded_trailing:
                return True

            next_confirmed_characters = (
                confirmed_characters + pending_characters + 1
            )
            if next_confirmed_characters > output_cap_chars:
                return True
            if pending_trailing:
                confirmed_parts.append("".join(pending_trailing))
                pending_trailing.clear()
                pending_characters = 0
            confirmed_parts.append(character)
            confirmed_characters = next_confirmed_characters
        return False

    while chunk := await stream.read(_RUN_CAPTURE_READ_CHUNK_BYTES):
        decoded = decoder.decode(chunk, final=False)
        if consume(decoded):
            return "", True
    decoded = decoder.decode(b"", final=True)
    if consume(decoded):
        return "", True
    return "".join(confirmed_parts), False


async def _read_run_capture_stderr(stream) -> tuple[bytes, bool]:
    """Drain stderr concurrently while retaining only a bounded log sample."""
    captured = bytearray()
    truncated = False
    while chunk := await stream.read(_RUN_CAPTURE_READ_CHUNK_BYTES):
        remaining = _RUN_CAPTURE_STDERR_LOG_BYTES - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    return bytes(captured), truncated


async def _drain_run_capture_stream(stream) -> None:
    """Discard remaining pipe data while a terminated child closes cleanly."""
    while await stream.read(_RUN_CAPTURE_READ_CHUNK_BYTES):
        pass


async def _collect_run_capture_output(
    process, output_cap_chars: int
) -> tuple[str, bytes, bool]:
    """Read both pipes without deadlock and stop as soon as stdout overflows."""
    stdout_task = asyncio.create_task(
        _read_run_capture_stdout(process.stdout, output_cap_chars)
    )
    stderr_task = asyncio.create_task(_read_run_capture_stderr(process.stderr))
    wait_task = asyncio.create_task(process.wait())
    tasks = (stdout_task, stderr_task, wait_task)
    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            if stdout_task in done:
                stdout, over_cap = stdout_task.result()
                if over_cap:
                    await _terminate_run_capture_process(
                        process, drain_streams=(process.stdout,)
                    )
                    await asyncio.gather(
                        stderr_task,
                        wait_task,
                        return_exceptions=True,
                    )
                    raise _RunCaptureOutputCapExceeded
        stdout, _over_cap = stdout_task.result()
        stderr, stderr_truncated = stderr_task.result()
        return stdout, stderr, stderr_truncated
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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

def words_to_int(text: Optional[str]) -> Optional[int]:
    """Convert word or digit string to integer for numeric parameter parsing.

    Supports both digit strings ("3") and word strings ("three", "five",
    "fifteen", "twenty three").
    Returns 1 as default if text is None (for commands like "go up" without a count).
    Returns None if text cannot be converted.

    The word reading is parse_number_word, the one word-to-integer
    implementation in this service (wh-number-words-one-parser). This
    function used to carry its own one..ten table, which is why a count
    above ten was refused, the command fired without it, and the count
    word was dictated. Homophones and the word "zero" come from that
    parser's documented options, not from a table here.

    This function does NOT cap the value. Each caller applies its own
    limit after the conversion -- press and hotkey clamp a repeat at 50,
    scroll clamps at MAX_SCROLL_CLICKS -- and those limits are unchanged.

    A digit string longer than ``sys.get_int_max_str_digits()`` (4300 by
    default in Python 3.12) cannot be converted either, and is reported as
    None like any other unreadable count. Python raises ValueError for that
    case rather than returning a value, and NO caller of this function
    catches it (wh-voice-access-parity.2.3.2.6).

    Args:
        text: String to convert (e.g., "3", "three", "five"), or None for default

    Returns:
        Integer value if conversion succeeds, 1 if text is None, None if invalid
    """
    if text is None: return 1
    text = str(text).lower().strip()
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            # Only the digit-count limit reaches here: str.isdigit has
            # already ruled out every other way int() can refuse a string.
            # Letting the exception escape breaks both callers, in
            # opposite directions. PatternMatcher.validate_numeric catches
            # ValueError and returns True, so the capture is reported
            # VALID. CommandEngine._execute_rule's outer handler catches
            # it, logs a traceback at error level, and abandons the rule.
            # Log the LENGTH, never the digits: a count is transcribed
            # speech (wh-voice-access-parity.2.3.2.6).
            logger.debug(
                "words_to_int: a %d-digit count is too long to convert; "
                "reporting it unreadable",
                len(text),
            )
            return None
    return parse_number_word(text, aliases=True, zero=True)

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

    # Arrow keys. VK_CODE_MAP already holds the bare names up, down, left
    # and right, but nobody says "press up" -- David reported "press up
    # arrow" and "press down arrow" doing nothing, because press_keys read
    # "arrow" as a second key name and gave up on the whole phrase
    # (wh-arrow-key-names-missing). The two-word lookup in press_keys runs
    # before the single-word lookup, so these entries also make
    # "control left arrow" resolve to ctrl plus left.
    "up arrow": "up",
    "down arrow": "down",
    "left arrow": "left",
    "right arrow": "right",

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
        self._functions["scroll"] = self.scroll
        self._functions["start_continuous_scroll"] = self.start_continuous_scroll
        self._functions["stop_continuous_scroll"] = self.stop_continuous_scroll
        self._functions["press"] = self.press
        self._functions["press_keys"] = self.press_keys
        self._functions["activate"] = self.activate_window
        self._functions["literal"] = self.handle_literal
        self._functions["type_text"] = self.type_text
        self._functions["insert_text"] = self.insert_text
        self._functions["insert_raw"] = self.insert_raw
        self._functions["select_phrase"] = self.select_phrase
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
        self._functions["open_url"] = self.open_url
        self._functions["run_capture"] = self.run_capture
        self._functions["capture_clipboard"] = self.capture_clipboard
        self._functions["add_hint_to_stt"] = self.add_hint_to_stt
        self._functions["cursor_navigate"] = self.cursor_navigate
        self._functions["click_element"] = self.click_element
        self._functions["show_overlay_command"] = self.show_overlay_command
        self._functions["hide_overlay_command"] = self.hide_overlay_command
        # Mouse grid + click gestures (wh-grid-speech-routing)
        self._functions["click_element_command"] = self.click_element_command
        self._functions["grid_show_command"] = self.grid_show_command
        self._functions["grid_dismiss_command"] = self.grid_dismiss_command
        self._functions["grid_next_screen_command"] = self.grid_next_screen_command
        self._functions["grid_action_command"] = self.grid_action_command
        self._functions["grid_number_command"] = self.grid_number_command
        self._functions["grid_click_command"] = self.grid_click_command
        # AI Service
        self._functions["fix_text_ai"] = self.fix_text_ai
        self._functions["rewrite_text_ai"] = self.rewrite_text_ai
        self._functions["ask_ai"] = self.ask_ai
        self._functions["cancel_fix"] = self.cancel_fix
        #self._functions["wheelhouse_help"] = self.wheelhouse_help
        self._functions["wheelhouse_help_online"] = self.wheelhouse_help_online
        # Pattern Manager
        self._functions["open_pattern_manager"] = self.open_pattern_manager
        # Voice calibration (wh-7ou.7.2.4)
        self._functions["open_calibration"] = self.open_calibration
        # Mode switching
        self._functions["set_speech_interaction_mode"] = self.set_speech_interaction_mode
        self._functions["stop_listening"] = self.stop_listening

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

    def select_phrase(self, phrase):
        """Select the first match of a spoken phrase in the focused control.

        The search and the selection both happen in the Input process,
        inside the target application, through the UI Automation text
        pattern. This function only packages the words. It does not read
        the document, so no document text crosses a process boundary.

        The match is exact apart from letter case. The UI Automation
        search ignores case, and nothing here changes the words. A phrase
        whose written form holds punctuation will not match: the spoken
        "hello world" does not find the written "hello, world".

        Args:
            phrase: The words captured after the command word.

        Returns:
            The payload for the Input process, or None when the capture
            holds no words. None declines the command and lets the words
            fall through to dictation, the same way click_element declines.
        """
        if not phrase:
            return None
        cleaned = phrase.strip()
        if not cleaned:
            return None
        return {"action": "select_phrase", "params": {"phrase": cleaned}}

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

    def scroll(self, direction, count=None):
        """Turn the mouse wheel a number of notches (wh-voice-access-parity.2.3).

        :flow: Command and Dictation Routing
        :step: 4e
        :produces_for: UI Action Execution
        :description: Builds the payload for one discrete mouse wheel scroll.
        :data_in: direction ("up", "down", "left" or "right"), count (optional
            capture group holding a spoken number as text).
        :data_out: Dict payload
            ``{"action": "scroll_wheel", "params": {"direction": str,
            "clicks": int}}``, or None when the direction is unusable or the
            count is an explicit zero.

        The count follows the same rules the hotkey repeat count already
        follows, because it arrives the same way -- from an optional regex
        group that may not have matched:

        - None, the string "None" and an empty string all mean one notch.
        - A digit string converts through words_to_int, up to the digit
          count Python is willing to convert: sys.get_int_max_str_digits(),
          4300 by default. A longer run is unreadable and takes the
          one-notch fallback below. It never reaches this function from
          speech in any event, because the pattern's numeric validation
          rejects it first and the utterance becomes dictation
          (wh-voice-access-parity.2.3.2.6).
        - A spoken number WORD converts too, through the same
          parse_number_word the rest of the service uses, so "eleven" and
          "one hundred and twenty three" both arrive here as counts. That
          helper carried its own one..ten table until
          wh-number-words-one-parser; a word above ten used to fail the
          pattern's numeric validation and the utterance became dictation.
        - An explicit zero sends NOTHING and returns None. Zero is a number
          the user said on purpose, and the honest answer to "scroll down
          zero" is to scroll nothing (wh-voice-access-parity.2.3.2.1).
        - A count this function cannot read falls back to one notch rather
          than refusing. That is a recognition failure, not an instruction:
          the user asked to scroll, so scrolling once beats doing nothing.
          "-4" lands here rather than in the zero case, because words_to_int
          reads a count with str.isdigit and returns None for a minus sign.
          A negative cannot arrive from speech in any event: the widened word
          capture holds no minus sign.
        - A count above MAX_SCROLL_CLICKS is CLAMPED here rather than refused,
          matching the hotkey repeat cap. The SendInput primitive still
          refuses an out-of-range count, because at that layer the value can
          only have come from a malformed message rather than from speech.

        An unknown direction returns None, which the command engine treats as
        "nothing to send", so a malformed pattern cannot scroll in some
        arbitrary default direction.
        """
        from utils.win_input_sender import MAX_SCROLL_CLICKS

        if not isinstance(direction, str):
            logger.warning("scroll: non-text direction %r; sending nothing", direction)
            return None
        normalized = direction.strip().lower()
        if normalized not in ("up", "down", "left", "right"):
            logger.warning("scroll: unknown direction %r; sending nothing", direction)
            return None

        clicks = 1
        if count is not None and str(count) not in ("None", ""):
            parsed = words_to_int(str(count))
            if parsed is not None:
                if parsed <= 0:
                    logger.debug(
                        "scroll: count %r means no notches; sending nothing",
                        count,
                    )
                    return None
                clicks = min(parsed, MAX_SCROLL_CLICKS)

        logger.debug("Scroll: %s, clicks: %d", normalized, clicks)
        return {
            "action": "scroll_wheel",
            "params": {"direction": normalized, "clicks": clicks},
        }

    def start_continuous_scroll(self, direction):
        """Start a scroll that keeps going (wh-voice-access-parity.2.3.3).

        :flow: Command and Dictation Routing
        :step: 4e
        :produces_for: UI Action Execution
        :description: Builds the payload that starts the repeating wheel timer.
        :data_in: direction ("up", "down", "left" or "right").
        :data_out: Dict payload ``{"action": "start_continuous_scroll",
            "params": {"direction": str}}``, or None when the direction is
            unusable.

        No timer runs here. The timer lives in the Input process, which owns
        SendInput and receives the stop word, so this function only names the
        direction and returns.

        An unknown direction returns None, which the command engine treats as
        "nothing to send". That refusal matters more here than it does for a
        discrete scroll: a timer started in some arbitrary default direction
        would keep scrolling until the user found the words to stop it.
        """
        if not isinstance(direction, str):
            logger.warning(
                "start_continuous_scroll: non-text direction %r; sending "
                "nothing", direction,
            )
            return None
        normalized = direction.strip().lower()
        if normalized not in ("up", "down", "left", "right"):
            logger.warning(
                "start_continuous_scroll: unknown direction %r; sending "
                "nothing", direction,
            )
            return None

        logger.debug("Continuous scroll start: %s", normalized)
        return {
            "action": "start_continuous_scroll",
            "params": {"direction": normalized},
        }

    def stop_continuous_scroll(self):
        """Stop the scroll that keeps going (wh-voice-access-parity.2.3.3).

        :flow: Command and Dictation Routing
        :step: 4e
        :produces_for: UI Action Execution
        :description: Builds the payload that stops the repeating wheel timer.
        :data_in: nothing.
        :data_out: Dict payload ``{"action": "stop_continuous_scroll",
            "params": {}}``.

        It takes no direction. Stopping is the same act whichever way the
        scroll was going, and a stop that carried a direction could refuse to
        stop a scroll going the other way.

        It never returns None. Stopping a scroll that already stopped changes
        nothing, and a user who says the words twice must not have the second
        one refused.
        """
        logger.debug("Continuous scroll stop")
        return {"action": "stop_continuous_scroll", "params": {}}

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
        If any key is unrecognized, raises ActionFailed. The rule engine
        catches it, abandons the rule, and the speech processor sends the
        spoken words to dictation instead of dropping them.

        Tuple entries in SPOKEN_KEY_MAP (e.g., ("shift", "9") for parenthesis)
        are expanded into the key list.

        Args:
            key_sequence: Space-separated key names (e.g., "control alt delete")

        Returns:
            Dictionary payload for hotkey action

        Raises:
            ActionFailed: the key sequence is empty or names a key this
                build does not know.
        """
        if not key_sequence:
            logger.warning("press_keys: Empty key sequence")
            raise ActionFailed("press_keys: empty key sequence")

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
                # Unrecognized key - abandon the whole command. Raising
                # (rather than returning None) is what makes the rule
                # engine report failure, so the words really do reach
                # dictation (wh-arrow-key-names-missing).
                logger.info(
                    "press_keys: Unrecognized key '%s'; sending the spoken "
                    "words to dictation",
                    redact_transcript(words[i]),
                )
                raise ActionFailed(
                    f"press_keys: unrecognized key "
                    f"{redact_transcript(words[i])!r}"
                )
            i += 1

        if not normalized_keys:
            logger.warning("press_keys: No keys parsed")
            raise ActionFailed("press_keys: no keys parsed")

        # Sort: modifiers first, then other keys (order-independent)
        modifiers = [k for k in normalized_keys if k in _MODIFIER_KEYS]
        non_modifiers = [k for k in normalized_keys if k not in _MODIFIER_KEYS]
        sorted_keys = modifiers + non_modifiers

        logger.debug(f"press_keys: '{key_sequence}' -> {sorted_keys}")
        # Build the payload here rather than calling hotkey(). hotkey()
        # takes its last argument as a repeat count whenever words_to_int
        # returns a positive integer, and every digit '0'-'9' is a real
        # key name in VK_CODE_MAP. Routing through it swallowed the
        # trailing digit: "press 1" produced an empty key list, "press
        # control 2" pressed ctrl twice and never pressed 2, and the
        # ("shift", "9") aliases for "(" pressed shift nine times. Every
        # token here is already validated as a key name, so the repeat is
        # always 1 (wh-arrow-key-names-missing.1.2). hotkey() keeps the
        # heuristic for pattern callers that pass a bare count group, such
        # as the "undo 3" pattern's hk ctrl z g1.
        return {
            "action": "hotkey_action",
            "params": {"keys": [str(k) for k in sorted_keys], "repeat": 1},
        }

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
        try:
            opened = await asyncio.to_thread(webbrowser.open, url)
            if not opened:
                # webbrowser.open reports the common failure shape by
                # returning False (on Windows it catches the OSError from
                # os.startfile), not by raising (wh-open-url-action.1.2).
                # The URL carries spoken text, so it goes through
                # transcript redaction (wh-open-url-action.1.5).
                logger.error(
                    "GSearch: browser reported failure opening %s",
                    redact_transcript(url[:200]),
                )
        except Exception as e:
            logger.error(
                "GSearch error (%s): %s",
                type(e).__name__, redact_transcript(str(e)),
            )
        return None
    async def open_url(self, url_template=None):
        """Open a URL in the default browser (wh-open-url-action, spec 5).

        The command engine URL-encodes every value it substitutes into the
        parameter before this method runs, so the scheme and host must be
        written in the pattern's template -- spoken text cannot supply them.

        Args:
            url_template: The URL after substitution. Must start with
                http:// or https:// (case-insensitive); anything else
                raises ValueError so the engine stops the pattern's
                remaining steps (spec rule 2.2). A browser-launch failure
                only logs (spec 5.3), matching GSearch.
        """
        url = "" if url_template is None else str(url_template)
        if not url.lower().startswith(("http://", "https://")):
            # The URL may carry spoken or clipboard text and this message
            # is logged by the engine's rule-failure handler, so the
            # content goes through transcript redaction
            # (wh-open-url-action.1.5).
            raise ValueError(
                "open_url: URL must start with http:// or https:// after "
                f"substitution; got {redact_transcript(url[:80])}"
            )
        try:
            opened = await asyncio.to_thread(webbrowser.open, url)
            if not opened:
                # Same false-return failure shape as GSearch above
                # (wh-open-url-action.1.2); spec 5.3 makes launch failure
                # log-only, not silent. Redacted for the same reason as
                # the bad-scheme message.
                logger.error(
                    "open_url: browser reported failure opening %s",
                    redact_transcript(url[:200]),
                )
        except Exception as e:
            logger.error(
                "open_url launch error (%s): %s",
                type(e).__name__, redact_transcript(str(e)),
            )
        return None

    async def run_capture(self, *params: Any) -> str:
        """Run a program without a shell and return its captured stdout.

        A leading finite TOML number is a timeout in seconds. All remaining
        parameters are passed to ``create_subprocess_exec`` as distinct
        arguments, so captured speech cannot alter the executable or invoke a
        shell. Failures raise so CommandEngine stops later pattern steps.
        """
        actions_config = self._get_actions_config()
        timeout_s = actions_config.run_capture_timeout_default_s
        command_params = list(params)
        if command_params and not isinstance(command_params[0], bool):
            timeout_candidate = command_params[0]
            if isinstance(timeout_candidate, int):
                # TOML integers have arbitrary precision, so clamp before
                # float() to avoid OverflowError on authored huge values.
                timeout_s = float(
                    max(
                        RUN_CAPTURE_TIMEOUT_FLOOR_S,
                        min(timeout_candidate, RUN_CAPTURE_TIMEOUT_CEILING_S),
                    )
                )
                command_params.pop(0)
            elif isinstance(timeout_candidate, float) and math.isfinite(
                timeout_candidate
            ):
                timeout_s = max(
                    RUN_CAPTURE_TIMEOUT_FLOOR_S,
                    min(timeout_candidate, RUN_CAPTURE_TIMEOUT_CEILING_S),
                )
                command_params.pop(0)

        if not command_params:
            raise ValueError("run_capture: a program path is required")
        if command_params[0] is None:
            raise ValueError("run_capture: a program path is required")
        if any(value is None for value in command_params):
            raise ValueError(
                "run_capture: unresolved parameter; refusing to run with a "
                "missing capture"
            )
        if any(
            not isinstance(value, (str, int, float)) or isinstance(value, bool)
            for value in command_params
        ):
            raise ValueError(
                "run_capture: parameters must be str, int, or float values"
            )

        command = [str(value) for value in command_params]
        redacted_command = redact_transcript(" ".join(command))
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            logger.warning(
                "run_capture program was not found; command=%s", redacted_command
            )
            raise FileNotFoundError(
                exc.errno,
                "run_capture: program was not found",
                redacted_command,
            ) from None
        except OSError as exc:
            logger.warning(
                "run_capture program could not start (%s); command=%s",
                type(exc).__name__,
                redacted_command,
            )
            raise RuntimeError("run_capture: program could not start") from None

        try:
            stdout_text, stderr, stderr_truncated = await asyncio.wait_for(
                _collect_run_capture_output(
                    process, actions_config.output_cap_chars
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            await _terminate_run_capture_process(
                process, drain_streams=(process.stdout, process.stderr)
            )
            logger.warning(
                "run_capture timed out after %.3gs; command=%s",
                timeout_s,
                redacted_command,
            )
            raise TimeoutError("run_capture: program timed out") from None
        except _RunCaptureOutputCapExceeded:
            logger.warning(
                "run_capture output exceeded output_cap_chars; command=%s",
                redacted_command,
            )
            raise ValueError(
                "run_capture: output exceeds output_cap_chars; refusing to "
                "store incomplete output"
            ) from None
        except asyncio.CancelledError:
            await _terminate_run_capture_process(
                process, drain_streams=(process.stdout, process.stderr)
            )
            raise
        except BaseException:
            await _terminate_run_capture_process(
                process, drain_streams=(process.stdout, process.stderr)
            )
            raise

        stderr_text = stderr.decode("utf-8", errors="replace")
        if stderr_truncated:
            stderr_text += " [stderr output truncated]"
        if process.returncode != 0:
            logger.warning(
                "run_capture exited with code %d; command=%s; stderr=%s",
                process.returncode,
                redacted_command,
                redact_transcript(stderr_text),
            )
            raise RuntimeError(
                f"run_capture: program exited with code {process.returncode}"
            )

        logger.debug(
            "run_capture completed; command=%s; stderr=%s",
            redacted_command,
            redact_transcript(stderr_text),
        )
        return stdout_text.rstrip("\r\n")

    def _get_actions_config(self) -> ActionsConfig:
        """Read the local [actions] block without allowing config errors to raise."""
        config_service = getattr(self.speech_handler, "config_service", None)
        if config_service is None:
            return ActionsConfig()
        try:
            return ActionsConfig.from_raw(config_service.get("actions", {}))
        except Exception:
            logger.error("[actions] config could not be read; using defaults")
            return ActionsConfig()

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
        
        This function is triggered by the "boost" voice command, spoken
        as the whole utterance. It:
        1. Captures the current clipboard content (which should contain selected text)
        2. Validates the hint text
        3. Sends a WebSocket command to the STT server to add the hint
        
        The STT provider saves the hint to the shared hints file
        (services/stt_providers/shared/hints.txt, through
        shared/hints_updater.py). Whether the running engine applies it
        depends on the engine; the command matches only when the engine
        reports that it applies hints or has not reported
        (wh-boost-engine-qualification).
        
        Usage in patterns.toml:
            [[pattern]]
            pattern = '''^boost$'''
            whole_utterance_only = true
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
            # wh-overlay-slow-uia-stale-badges.7.1.5: this fallback used
            # to send its intelligent_insert_text payload straight
            # through app.send_command, bypassing the screen-read gate,
            # the editor consult, and sentence tracking. Route it
            # through the processor helper like the grid dictation
            # fallback, and mark the parse as dictation so a later STT
            # revision can still retract the typed words. No raw send
            # remains: with no processor (partial wiring) the phrase is
            # dropped rather than typed around the gate.
            processor = getattr(
                self.speech_handler, "speech_processor", None,
            )
            parser = getattr(processor, "text_parser", None)
            if parser is not None:
                parser.dictation_fallback_this_parse = True
            send = getattr(processor, "_send_to_dictation", None)
            if send is None:
                logger.warning(
                    "cursor_navigate fallback: no speech processor "
                    "wired; dropping %r rather than bypassing the "
                    "screen-read gate",
                    redact_transcript(utterance),
                )
                return None
            try:
                await send(utterance)
            except Exception:
                # Same rationale as the grid dictation fallback
                # (wh-mouse-grid.1.28): a raise after submission would
                # turn this matched rule into a parse-level failure,
                # and the utterance would then dictate a second time.
                # A pre-enqueue delivery failure has already restored
                # the sentence event inside the helper.
                logger.exception(
                    "cursor_navigate fallback: insert of %r raised "
                    "after submission; not retrying",
                    redact_transcript(utterance),
                )
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

    async def click_element_command(self, utterance: str):
        """Execute a gesture click command (wh-grid-speech-routing).

        Handles the full-command forms "right click <target>" /
        "double click <target>" (and plain "click <target>" if a pattern
        ever routes it here) via ``ClickCommandParser.parse_command``, which
        carries the gesture onto the ``ElementQuery``. Mirrors
        ``click_element``: unparseable input returns None (nothing crosses
        the process boundary), otherwise delegate to
        ``LogicController.forward_click_element`` which owns the config
        gate, the overlay/grid number routing, and every degrade path.
        """
        import uuid

        from .click_parser import ClickCommandParser
        from utils.trace_context import set_trace, get_trace_id

        query = ClickCommandParser.parse_command(utterance)
        if query is None:
            logger.info(
                "click_element_command: unparseable utterance %r; ignoring",
                redact_transcript(utterance),
            )
            return None

        trace_id = get_trace_id() or f"click-{uuid.uuid4().hex[:12]}"
        set_trace(trace_id)

        lc = getattr(self.speech_handler, "logic_controller", None)
        if lc is None or not hasattr(lc, "forward_click_element"):
            logger.error(
                "click_element_command: no "
                "logic_controller.forward_click_element available; cannot "
                "execute (trace_id=%s)", trace_id,
            )
            return None

        logger.info(
            "click_element_command: parsed query name=%r role=%r gesture=%s "
            "trace_id=%s",
            query.name, query.role, query.gesture.value, trace_id,
        )
        await lc.forward_click_element(query, trace_id)
        return None

    async def grid_show_command(self):
        """Handle 'show grid' (wh-grid-speech-routing)."""
        return await self._delegate_grid_command("open")

    async def grid_dismiss_command(self):
        """Handle 'hide grid' (wh-grid-speech-routing)."""
        return await self._delegate_grid_command("dismiss")

    async def grid_next_screen_command(self):
        """Handle 'grid next screen' (wh-grid-speech-routing)."""
        return await self._delegate_grid_command("next_screen")

    async def grid_action_command(self, action: str, utterance: str):
        """Handle a bare grid-open-only word: mark / drag / move_here.

        ``action`` is the fixed command the pattern binds; ``utterance`` is
        the spoken text (with any trailing punctuation), used verbatim for
        the dictation fallback when the grid is closed -- these words are
        ordinary dictation vocabulary and must keep typing normally.
        """
        if action not in ("mark", "drag", "move_here"):
            logger.warning(
                "grid_action_command: unknown action %r; dictating %r",
                action, redact_transcript(utterance),
            )
            await self._dictate_grid_fallback(utterance)
            return None
        return await self._delegate_grid_command(action, utterance=utterance)

    async def grid_number_command(self, utterance: str, number_word: str):
        """Handle a bare 1..9 number ('five', '5', 'number five').

        With the grid open the number refines the grid. With the grid
        closed and the numbered overlay showing badges, the number
        clicks its badge (wh-overlay-count-homophones.1.1) -- the grid
        is asked first, the overlay only after it refuses. With neither
        showing, the whole utterance falls back to dictation, so the
        literal words keep typing. Numbers the parser cannot map to 1..9
        dictate as well (defensive; the pattern only captures one..nine,
        the homophones "to", "too" and "for", and a single digit).
        """
        from .number_word_parser import parse_number_word

        # wh-overlay-count-homophones: aliases, so the grid reads "too",
        # "to" and "for" as 2 and 4, matching the two word-form grid
        # patterns that now capture them. With the grid closed the number
        # goes to the numbered overlay first; it types only when neither
        # the grid nor the badges are on screen.
        number = parse_number_word(number_word, aliases=True)
        if number is None or not 1 <= number <= 9:
            await self._dictate_grid_fallback(utterance)
            return None
        return await self._delegate_grid_command(
            "refine", number=number, utterance=utterance,
            badge_words=utterance,
        )

    async def grid_click_command(self, utterance: str):
        """Handle a bare 'click' / 'right click' / 'double click'.

        Acts at the grid's current cell center when the grid is open;
        dictates the utterance when it is closed.
        """
        from .click_parser import ClickGesture

        lowered = utterance.strip().lower()
        if lowered.startswith("right"):
            gesture = ClickGesture.RIGHT_CLICK
        elif lowered.startswith("double"):
            gesture = ClickGesture.DOUBLE_CLICK
        else:
            gesture = ClickGesture.INVOKE
        return await self._delegate_grid_command(
            "click", gesture=gesture, utterance=utterance,
        )

    async def _delegate_grid_command(
        self, command: str, *, number: int = 0, gesture=None,
        utterance: Optional[str] = None, badge_words: Optional[str] = None,
    ):
        """Shared grid-command delegation (wh-grid-speech-routing).

        Delegates to ``LogicController.handle_grid_command`` and applies
        the dictation fallback: when the command is NOT consumed (a
        grid-open-only word with the grid closed) and ``utterance`` is
        provided, the utterance is inserted as dictation. Explicit grid
        commands pass no ``utterance`` and are never dictated.

        ``badge_words`` is the spoken number, and only the number
        commands pass it (wh-overlay-count-homophones.1.1). With it, the
        not-consumed fallback first offers the number to the numbered
        overlay: with badges showing, a bare number is a badge click,
        not text. The grid still gets the number first, so the two
        overlays keep the precedence they already had. The words type
        exactly as before when badges are not showing.
        """
        import uuid

        from utils.trace_context import set_trace, get_trace_id

        trace_id = get_trace_id() or f"grid-{uuid.uuid4().hex[:12]}"
        set_trace(trace_id)

        lc = getattr(self.speech_handler, "logic_controller", None)
        if lc is None or not hasattr(lc, "handle_grid_command"):
            logger.error(
                "grid %s: no logic_controller.handle_grid_command "
                "available (trace_id=%s)", command, trace_id,
            )
            if utterance:
                await self._dictate_grid_fallback(utterance)
            return None

        consumed = await lc.handle_grid_command(
            command, trace_id, number=number, gesture=gesture,
            spoken=utterance or "",
        )
        if not consumed and utterance:
            if badge_words and await self._click_badge_instead_of_dictating(
                badge_words, trace_id,
            ):
                return None
            logger.info(
                "grid %s: not consumed (grid closed); dictating %r "
                "(trace_id=%s)", command, redact_transcript(utterance),
                trace_id,
            )
            await self._dictate_grid_fallback(utterance)
        return None

    async def _click_badge_instead_of_dictating(
        self, spoken: str, trace_id: str,
    ) -> bool:
        """Click the numbered-overlay badge ``spoken`` names, if showing.

        wh-overlay-count-homophones.1.1, implementing David's ruling of
        2026-08-27 (wh-click-number-dictation.1.2): while badges are on
        screen, a bare number is a click, not text. The three grid
        patterns match the whole utterance for every spoken 1..9, so
        those words reach this action instead of the speech processor's
        bare-number hold, and the closed-grid fallback typed them.

        The click itself is NOT built here. The words go to the speech
        processor's existing bare-number click path
        (``try_bare_number_badge_click``), which owns the overlay gate,
        the "click N" command text and the retraction bookkeeping, so
        there is one click sender for both ways in.

        Returns True when the click ran and the caller must not dictate.
        """
        processor = getattr(self.speech_handler, "speech_processor", None)
        attempt = getattr(processor, "try_bare_number_badge_click", None)
        if attempt is None:
            # Partial wiring (no speech processor, or an older stub):
            # the words type, which is the behaviour before this fix.
            return False
        clicked = await attempt(spoken)
        if clicked:
            logger.info(
                "grid number: not consumed (grid closed) but badges are "
                "showing; clicked badge for %r instead of dictating "
                "(trace_id=%s)", redact_transcript(spoken), trace_id,
            )
        return clicked

    async def _dictate_grid_fallback(self, utterance: str):
        """Type a closed-grid fallback utterance as ordinary dictation.

        wh-mouse-grid.1.27: the bare grid words are dictation when the
        grid is closed -- and, for the number commands since
        wh-overlay-count-homophones.1.1, only when the numbered overlay
        is not showing badges either -- so they must ride the speech
        processor's normal dictation path (``_send_to_dictation``) -- which applies the
        terminal focus-redirect (``maybe_route_to_editor``) and the
        per-utterance editor bookkeeping the retraction path reads --
        instead of a raw fire-and-forget insert payload. The text parser
        is also told this parse ended in a dictation fallback, so
        ``parse_and_execute`` records the match as 'dictation_fallback'
        rather than 'command' and a later STT revision can still retract
        the typed words. Falls back to the raw insert payload only when
        no speech processor is available (partial wiring).
        """
        processor = getattr(self.speech_handler, "speech_processor", None)
        parser = getattr(processor, "text_parser", None)
        if parser is not None:
            parser.dictation_fallback_this_parse = True
        send = getattr(processor, "_send_to_dictation", None)
        try:
            if send is not None:
                await send(utterance)
            else:
                await self.speech_handler.app.send_command(
                    self.insert_text(utterance)
                )
        except Exception:
            # wh-mouse-grid.1.28: by the time the insert request can
            # raise (a late acknowledgement past the Logic timeout), it
            # has already been submitted to Input. Letting the error
            # escape turns this matched rule into a parse-level failure,
            # and _execute_command then dictates the same words a second
            # time -- both insertions can run. Absorb it: one submission
            # is the outcome of the fallback, acknowledged or not.
            logger.exception(
                "grid dictation fallback: insert of %r raised after "
                "submission; not retrying", redact_transcript(utterance),
            )

    async def open_pattern_manager(self):
        """Open the Pattern Manager dialog via GUI IPC."""
        self._send_gui_action({"action": "open_pattern_manager"})
        logger.info("Sent open_pattern_manager to GUI")
        return None

    async def open_calibration(self):
        """Open the Teach-WheelHouse-your-voice window via GUI IPC.

        wh-7ou.7.2.4 (spec Sections 3.1, 6.4): the "learn my voice" /
        "calibrate my voice" voice commands land here. The GUI opens the
        calibration window and answers with cal_session_open; the
        CalibrationController then decides the first screen (intro, or
        the wrong-provider notice when the active STT provider is not
        Distil-Whisper).
        """
        self._send_gui_action({"action": "open_calibration"})
        logger.info("Sent open_calibration to GUI")
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
                    "title": "Wheelhouse",
                    "message": display_name,
                    "timeout": 3,
                })
                logger.info(f"Speech interaction mode set to: {mode}")
            else:
                logger.warning("Cannot set interaction mode -- state_manager not available")
        else:
            logger.warning("Cannot set interaction mode -- logic_controller not available")
        return None

    def stop_listening(self):
        """Switch listening off: the "stop listening" voice command.

        Runs the disabling half of the floating-button toggle
        (StateManager.disable_speech_by_user), so the engine is told reason
        "manual" (wh-wake-word-stop-listening, boss ruling R3). In toggle
        mode that reason arms the detector and the wake word switches
        listening back on. In push-to-talk mode it does not: the wake word
        there ends only the idle pause
        (StateManager._wake_word_ends_idle_pause_only, boss ruling option
        2), so the way back is the next hold of the floating button. The
        help texts state that limit (wh-wake-word-stop-listening.1.2).

        disable_speech_by_user already shows the "Speech is off. You
        switched it off." notice, through the same send_notice toast the
        mode-switch commands reach with their
        show_notification GUI action. This function therefore sends no
        show_notification of its own: a second one would show the user two
        notices for one command. Do not add one (boss ruling 11:14
        2026-09-22, departure b).

        The phrase normally arrives only while listening is on, since
        nothing is transcribed while it is off. A transcript already in
        flight when a pause starts is the exception, and it changes nothing.
        """
        lc = getattr(self.speech_handler, 'logic_controller', None)
        sm = getattr(lc, 'state_manager', None) if lc else None
        if sm is None:
            logger.warning("Cannot stop listening -- state_manager not available")
            return None
        if not sm.speech_enabled:
            logger.info("Stop listening: listening is already off - nothing to do")
            return None
        sm.disable_speech_by_user("Voice command")
        logger.info("Listening stopped by voice command")
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

    @contextlib.asynccontextmanager
    async def _ai_cancel_lane(self):
        """Keep the speech processor's cancel-only lane open for one AI call.

        wh-cancel-fix-running-rewrite. The word-event loop is serial: it awaits
        one event's processing before it reads the next, and this action is
        that processing. Without this the words of "x-ray cancel fix" sit in
        word_queue for the whole model call and reach ``cancel_fix`` only after
        the replacement has been pasted, when ``ai.is_processing()`` is already
        False -- so the cancel sets no flag and shows no notice.

        While the lane is open the processor defers every word event it takes
        and acts on none of them EXCEPT the cancel command, which it runs at
        once. That keeps the property this serial loop has always had: nothing
        else types while the model runs.

        A processor that does not offer the lane, or an AI action reached from
        outside the word loop, simply runs as it did before.
        """
        processor = getattr(self.speech_handler, "speech_processor", None)
        begin = getattr(processor, "begin_ai_cancel_lane", None)
        end = getattr(processor, "end_ai_cancel_lane", None)
        if not callable(begin) or not callable(end):
            yield
            return
        begin()
        try:
            yield
        finally:
            end()

    def _notify_ai_status(self, message: str) -> None:
        """Keep AI outcome wording visible; screen readers own spoken feedback."""
        logger.info("AI: %s", message)
        self._send_gui_action({
            "action": "show_notification",
            "title": "Wheelhouse",
            "message": message,
        })

    async def fix_text_ai(self):
        """Capture text from focused element, correct via AI, paste back."""
        return await self._run_ai_text_transform(
            send=lambda ai, captured: ai.fix_text(captured),
            working_word="Correcting",
            no_text_message="No text to correct.",
            failed_message="Correction failed. Original text preserved.",
        )

    async def rewrite_text_ai(self, instruction: str):
        """Rewrite the selection in the style the pattern asked for.

        ``instruction`` is the pattern's single parameter -- "Rewrite this text
        in plain language...", "...as a pirate would say it", or anything a
        user writes in their own pattern file. Nothing about a style lives in
        this method; it is the same sequence as fix_text_ai with a different
        request and different wording.
        """
        if not instruction or not str(instruction).strip():
            logger.warning("rewrite_text_ai called with no instruction")
            return None
        return await self._run_ai_text_transform(
            send=lambda ai, captured: ai.rewrite_text(captured, instruction),
            working_word="Rewriting",
            no_text_message="No text to rewrite.",
            failed_message="Rewrite failed. Original text preserved.",
        )

    async def ask_ai(self, prompt: str) -> str:
        """Ask the configured AI one question and return its trimmed reply.

        Pattern-template substitution has already happened before this action
        is called. Every failure raises so CommandEngine stops later pattern
        steps rather than attempting to use an absent or partial reply.
        """
        if not prompt or not str(prompt).strip():
            logger.warning("ask_ai failed: prompt is blank or missing")
            raise ValueError("ask_ai: prompt is blank or missing")

        ai = self._get_ai_service()
        if ai is None:
            logger.warning("ask_ai failed: AI service unavailable")
            raise RuntimeError("ask_ai: AI service unavailable")
        if not ai.is_ready():
            logger.warning(
                "ask_ai failed: AI subsystem is not configured or unavailable"
            )
            raise RuntimeError("ask_ai: AI subsystem is not configured or unavailable")
        if ai.is_processing():
            logger.warning("ask_ai failed: processing lock busy")
            raise RuntimeError("ask_ai: processing lock busy")

        timeout_s = min(ai.get_request_timeout_s(), 60.0)
        # This request names itself, so a provider switch started while
        # it runs keeps its own "Loading <engine>" dialog when this
        # request finishes (wh-dialog-ownership-token).
        from shared.dialog_owner import next_ai_owner_token

        owner = next_ai_owner_token()
        async with ai._processing_lock:
            logger.info("Asking.")
            self._send_gui_action(
                {"action": "show_working", "message": "Asking...", "owner": owner}
            )
            try:
                try:
                    # wh-cancel-fix-running-rewrite: the lane spans the
                    # model await, so "cancel fix" spoken while this
                    # question is out reaches cancel_fix. AIService.ask
                    # reads the flag after the provider call and returns
                    # CANCELLED, which the check below turns into the
                    # action's cancelled failure.
                    async with self._ai_cancel_lane():
                        reply = await asyncio.wait_for(
                            ai.ask(prompt), timeout=timeout_s,
                        )
                except asyncio.TimeoutError:
                    logger.warning("ask_ai failed: request timed out")
                    raise TimeoutError("ask_ai: request timed out") from None
                except Exception as exc:  # noqa: BLE001 -- action failure contract
                    logger.warning(
                        "ask_ai failed: AI request raised %s", type(exc).__name__
                    )
                    raise RuntimeError("ask_ai: AI request failed") from exc

                if reply.status is ChatStatus.CANCELLED:
                    logger.warning("ask_ai failed: request cancelled")
                    raise RuntimeError("ask_ai: request cancelled")

                if not reply.ok:
                    logger.warning(
                        "ask_ai failed: AI request not ok (status=%s)",
                        reply.outcome,
                    )
                    raise RuntimeError("ask_ai: AI request not ok")

                text = reply.text.strip()
                if len(text) > self._get_actions_config().output_cap_chars:
                    logger.warning("ask_ai failed: reply exceeds output_cap_chars")
                    raise ValueError("ask_ai: reply exceeds output_cap_chars")
                return text
            finally:
                # cancel_fix only sets this under the processing lock, so it
                # targets this ask_ai request. Clear it on every exit to
                # prevent stale cancellation from pre-cancelling the next fix.
                ai.cancel_requested = False
                self._send_gui_action({"action": "hide_working", "owner": owner})

    async def _run_ai_text_transform(
        self,
        send,
        *,
        working_word: str,
        no_text_message: str,
        failed_message: str,
    ):
        """Capture the selection, send it somewhere, paste the answer back.

        Every AI command that transforms the selected text runs this same
        sequence: check the service is there and ready, take the processing
        lock, capture, send, check for a cancellation that raced the answer,
        paste. Only three things differ between the correcting command and the
        rewriting ones, and they are the three keyword arguments -- the request
        itself plus the status wording shown to the user.

        Args:
            send: Called with the AIService and the captured text, returning
                an awaitable ChatResult. This is the whole difference between
                correcting and rewriting. It receives the service rather than
                looking it up again so there is exactly one lookup per command
                and the caller cannot be handed a different one mid-sequence.
            working_word: Present participle shown while waiting,
                for example "Correcting".
            no_text_message: Shown when the selection is empty.
            failed_message: Shown when the server answered but the request
                did not succeed, and the server is still reachable.
        """
        ai = self._get_ai_service()
        if not ai:
            logger.warning("AI text transform: AIService not available")
            return None

        # Readiness gate moved here (finding 1.9). When AI is off / unreachable
        # show a graceful notice instead of failing silently (design s7).
        if not ai.is_ready():
            self._notify_ai_status("AI is not available right now.")
            return None

        if ai.is_processing():
            self._notify_ai_status("Already processing, please wait.")
            return None

        # This transform names itself, so a provider switch or another
        # AI operation started while it runs keeps its own dialog when
        # this one finishes (wh-dialog-ownership-token). The token is
        # bound before the lock rather than inside the try below,
        # because the close in that try's finally runs on paths that
        # raised before the show -- a failed copy, an empty selection --
        # and a name bound inside the try would not exist for them. A
        # close naming a token nothing owns is simply dropped, where the
        # unnamed close it replaces tore down whatever dialog was up.
        from shared.dialog_owner import next_ai_owner_token

        owner = next_ai_owner_token()
        async with ai._processing_lock:
            # Step 1: Capture text via Input Process
            result = await self.speech_handler.app.send_request(
                "capture_selected_text", params={}
            )

            # wh-review-pattern-fixes.26: the capture flushes the
            # deferred letter buffer before it touches the clipboard or
            # the selection, and fails closed. When the flush failed,
            # the Input process changed nothing on screen; abort the
            # whole transform the same way.
            if result.get("flush_failed"):
                logger.warning(
                    "AI text transform: letter-buffer flush failed "
                    "before capture; transform aborted."
                )
                self._notify_ai_status(
                    "Earlier dictated letters were not delivered. "
                    "Original text preserved."
                )
                return None

            # wh-review-pattern-fixes.28: when the capture used the
            # Ctrl+A no-selection fallback, the whole field is still
            # selected after the copy. Every exit below that does not
            # replace the captured text must collapse that selection,
            # or the next dictated insertion overwrites the entire
            # field. A selection the user made themselves arrives with
            # select_all_fallback False and is left alone.
            fallback_selection_armed = bool(result.get("select_all_fallback"))
            # wh-review-pattern-fixes.45: the capture names the control
            # it read the selection from. The token resolves to that
            # control inside the Input process (a UIA control object
            # cannot cross the process boundary); the HWND is a plain
            # integer that can. Both go back with the replacement, and
            # the Input process refuses to paste unless both still match
            # a target that holds the foreground. The user can switch
            # windows while the model runs, and this is what stops the
            # correction from overwriting the new window's selection.
            capture_token = result.get("capture_token")
            capture_target_hwnd = result.get("target_hwnd")
            replaced = False
            try:
                # wh-review-pattern-fixes.35: copy_failed means a Ctrl+C
                # chord short-delivered in the Input process. The
                # selection state is UNKNOWN, not empty -- showing the
                # no-text wording would be a false result. Abort with a
                # copy-failure notice. The check sits inside the try so
                # the finally still collapses a fallback-armed
                # whole-field selection (the fallback copy can fail
                # AFTER a verified Ctrl+A armed it).
                if result.get("copy_failed"):
                    logger.warning(
                        "AI text transform: selection copy delivery "
                        "failed; transform aborted."
                    )
                    self._notify_ai_status(
                        "Could not copy the text. "
                        "Original text preserved."
                    )
                    return None

                # wh-review-pattern-fixes.38: capture_failed is the
                # catch-all for every capture-state failure that has no
                # more specific flag -- a failed sentinel clipboard
                # write, a short Ctrl+A, or a swallowed exception. Each
                # of those used to return the exact shape of a genuine
                # empty selection, so the branch below showed the
                # ordinary no-text message although the Input process
                # never established whether anything was selected. Like
                # the copy_failed check, this sits inside the try so
                # the finally still collapses a fallback-armed
                # whole-field selection.
                if result.get("capture_failed"):
                    logger.warning(
                        "AI text transform: the selection capture "
                        "failed; transform aborted."
                    )
                    self._notify_ai_status(
                        "Could not capture the text. "
                        "Original text preserved."
                    )
                    return None

                text = result.get("text", "")
                logger.debug("AI text transform: captured %d chars", len(text))
                if not text or not text.strip():
                    self._notify_ai_status(no_text_message)
                    return None

                # Step 2: Word-count notice for large text (no time
                # estimate -- the thin client has no local-tier basis for
                # a seconds estimate, so the estimate_correction_time call
                # and 'roughly N seconds' wording were dropped; design s4).
                word_count = len(text.split())
                if word_count > 200:
                    self._notify_ai_status(f"About {word_count} words.")

                logger.info("%s.", working_word)
                self._send_gui_action(
                    {
                        "action": "show_working",
                        "message": f"{working_word}...",
                        "owner": owner,
                    }
                )
                # wh-cancel-fix-running-rewrite: the cancel-only lane is
                # open from the moment the request goes out until this
                # code has decided whether to paste. While it is open the
                # speech processor defers every word event and acts on
                # none of them except the cancel command, so "cancel
                # fix" spoken during the model call reaches cancel_fix
                # instead of waiting behind this await.
                async with self._ai_cancel_lane():
                    # Step 3: Send to the AI (returns a ChatResult).
                    corrected = await send(ai, text)

                    # Step 3a: Cancellation is a distinct outcome -- do NOT probe
                    # the server or show an error (finding wh-ay6h.6.4).
                    if corrected.status is ChatStatus.CANCELLED:
                        self._notify_ai_status("Cancelled.")
                        return None

                    if not corrected.ok:
                        logger.warning(
                            "AI text transform: not ok (status=%s)",
                            corrected.outcome,
                        )
                        # MODEL_NOT_FOUND means the server responded (404 on
                        # the model), so a reachability re-probe would only
                        # mislead -- name the real problem instead (wh-75m).
                        if corrected.status is ChatStatus.MODEL_NOT_FOUND:
                            self._notify_ai_status(
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
                            self._notify_ai_status(
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
                            self._notify_ai_status(
                                "The AI server isn't responding. "
                                "Original text preserved."
                            )
                        else:
                            self._notify_ai_status(failed_message)
                        return None

                    corrected_text = corrected.text

                    # Step 4: Check cancellation before pasting (race between the
                    # AI response arriving and a concurrent cancel_fix call).
                    #
                    # crewcut: this check is the last point at which a
                    # cancel can stop the paste, and the lane closes
                    # here. A cancel spoken after the
                    # replace_selected_text request below is already in
                    # flight cannot recall it. To remove the limit the
                    # Input process would have to accept a cancel for a
                    # paste it has accepted but not yet delivered;
                    # nothing in that channel does today.
                    #
                    # The check does not clear the flag. The finally below
                    # clears it on every exit from this call, including
                    # this one, so a clear here as well would be a second
                    # clear no test could tell from the first -- and a
                    # mutation gate cannot honestly claim to guard a line
                    # whose removal changes nothing.
                    if ai.cancel_requested:
                        self._notify_ai_status("Cancelled.")
                        return None

                # Step 5: Replace with the answer via Input Process
                if corrected_text != text:
                    # wh-overlay-slow-uia-stale-badges.7.1.8: the rule
                    # pre-scan refused the command while a read was in
                    # flight at parse time, but a read can also BEGIN
                    # during the model await above. Re-check here: sent
                    # anyway, the paste would queue behind the read in
                    # Input's one command loop and land seconds late.
                    # The window-handle and capture-token checks defend
                    # the destination, not the timing, so the gate's
                    # drop-never-queue rule applies. The answer is
                    # discarded (the marker caps at
                    # _READ_GATE_MAX_REFUSAL_S, so this is a bounded
                    # race, and the status notice tells the user).
                    paste_processor = getattr(
                        self.speech_handler, 'speech_processor', None,
                    )
                    paste_refuses = getattr(
                        paste_processor, '_screen_read_refuses_dictation',
                        None,
                    )
                    if callable(paste_refuses) and paste_refuses() is True:
                        logger.info(
                            "AI text transform: a screen read started "
                            "while the model ran; the replacement is "
                            "dropped, not queued."
                        )
                        self._notify_ai_status(
                            "A screen read was in progress. Nothing was "
                            "pasted, and your original text is unchanged."
                        )
                        return None
                    replace_result = await self.speech_handler.app.send_request(
                        "replace_selected_text",
                        params={
                            "text": corrected_text,
                            "target_hwnd": capture_target_hwnd,
                            "capture_token": capture_token,
                        },
                    )
                    # wh-review-pattern-fixes.28: replace_selected_text
                    # returns success False when the clipboard write or
                    # the paste failed. Nothing was pasted, so the
                    # captured text is still on screen -- report the
                    # failure instead of reporting Done.
                    if not replace_result or not replace_result.get("success"):
                        # wh-review-pattern-fixes.45: focus_drift is a
                        # distinct outcome and needs its own words. The
                        # Input process refused because the window the
                        # selection came from no longer holds the
                        # foreground. It wrote no clipboard and sent no
                        # key, and it attempted no repair, so the user's
                        # text is exactly as they left it. Say that.
                        if replace_result and replace_result.get(
                            "focus_drift"
                        ):
                            logger.warning(
                                "AI text transform: the captured target "
                                "lost the foreground; nothing was pasted."
                            )
                            self._notify_ai_status(
                                "The window changed while the AI worked. "
                                "Nothing was pasted, and your original "
                                "text is unchanged."
                            )
                            return None
                        logger.warning(
                            "AI text transform: replacement paste failed"
                        )
                        self._notify_ai_status(
                            "Could not paste the corrected text. "
                            "Original text preserved."
                        )
                        return None
                    replaced = True
                    self._notify_ai_status("Done.")
                else:
                    self._notify_ai_status("No changes needed.")
            finally:
                # wh-cancel-fix-running-rewrite.1.1: the lane can run
                # cancel_fix while this call still holds the processing
                # lock, so the flag it sets belongs to THIS call. Step 3a
                # and the step 4 check consume it on the paths that end
                # normally, but every not-ok exit returns without reading
                # it -- and the one that matters awaits recheck_ready
                # inside the lane, which is the longest window a user has
                # to say "cancel fix". A flag left set here is consumed
                # by AIService._transform_text BEFORE the next provider
                # call, so the user's next rewrite would silently do
                # nothing. ask_ai clears it on every exit for the same
                # reason; this is that clear. cancel_fix sets the flag
                # only while is_processing() is true, which is this same
                # lock, so nothing later can be discarded here.
                ai.cancel_requested = False
                self._send_gui_action({"action": "hide_working", "owner": owner})
                # wh-review-pattern-fixes.28: collapse the fallback-armed
                # whole-field selection on every outcome that did not
                # replace the captured text (no text, cancelled, AI
                # failure, unchanged response, failed paste, exception).
                # A successful paste consumed the selection, and a user
                # selection never arms the flag.
                # wh-review-pattern-fixes.32 (c): the collapse is an
                # acknowledged request now. When the Right press was not
                # acknowledged, the whole-field selection may still be
                # armed and the next dictated insertion would overwrite
                # the entire field -- do not complete silently; warn the
                # user (the status notice is this file's convention for
                # user-visible danger states, e.g. the failed-paste
                # branch above).
                if fallback_selection_armed and not replaced:
                    collapsed = await self._collapse_fallback_selection()
                    if not collapsed:
                        logger.error(
                            "AI text transform: fallback-selection "
                            "collapse was not acknowledged; the "
                            "whole-field selection may still be active."
                        )
                        self._notify_ai_status(
                            "Warning: the text may still be selected."
                        )

        return None

    async def _collapse_fallback_selection(self) -> bool:
        """Collapse the capture's Ctrl+A whole-field selection.

        wh-review-pattern-fixes.28: capture_selected_text's no-selection
        fallback selects the whole field with Ctrl+A, and the copy does
        not collapse that selection. Press Right once. In standard
        Windows edit controls -- Win32 EDIT and RichEdit, Qt text
        widgets, and browser/Electron text fields -- Right collapses a
        selection to its end without modifying the text, which is why
        it is the cross-editor-safe choice over Escape
        (application-defined) or a click (position-dependent).

        wh-review-pattern-fixes.32 (c): the press goes through the
        acknowledged ``press_key_verified`` request (the same
        request/response channel capture_selected_text uses), not the
        enqueue-only send_command channel -- send_command has no
        response tracking and press_key_action discards the SendInput
        count, so a dropped Right would leave the selection armed while
        Logic believed cleanup completed. The Input handler invalidates
        the shadow buffer, so the next dictated word re-syncs against
        the moved caret.

        Returns:
            True only when the Input process acknowledged that the
            Right press was fully accepted by SendInput. False on a
            reported short send AND on a failed request (timeout, dead
            channel) -- both leave the selection state unknown.
        """
        try:
            result = await self.speech_handler.app.send_request(
                "press_key_verified", params={"key": "right", "repeat": 1}
            )
        except Exception as exc:  # noqa: BLE001 -- unknown state, report False
            logger.error(
                "collapse_fallback_selection: press_key_verified "
                "request failed: %s", exc,
            )
            return False
        return bool(result and result.get("success"))

    async def cancel_fix(self):
        """Set cancellation flag. Checked between AI response and paste."""
        ai = self._get_ai_service()
        if ai and ai.is_processing():
            ai.cancel_requested = True
            self._notify_ai_status("Cancelling.")
        return None

    """ async def wheelhouse_help(self, question: str = ""):

        payload = {"action": "show_help_chat"}
        if question:
            payload["question"] = question
        self._send_gui_action(payload)
        return None """

    async def wheelhouse_help_online(self):
        """Open the Wheelhouse Assistant, explaining it first when needed.

        The decision lives in LogicController.start_help_online, which the
        Help menu entry also reaches. Keeping it there is what stops the
        spoken command and the menu entry behaving differently: a user who
        says "help" meets the same explanation window, and turning that
        window off turns it off for both.
        """
        lc = getattr(self.speech_handler, "logic_controller", None)
        if not lc:
            return None

        await lc.start_help_online(source="spoken")
        return None
